# CityGML: buildings as an SVG plan and a shaded 3D plot

Geospatial formats still need a rendering path. This workflow compiles the
official **OGC CityGML 2.0**
schema closure once, binds real `CityModel` documents into pyxsd's typed
tree, walks the GML solids, and produces two server-side artifacts with no
GIS stack and no JavaScript:

- **`plan_svg.py`** — a top-down SVG plan: every surface ring projected to
  `x`/`y`, buildings filled by normalized `bldg:measuredHeight`.
- **`plot_city3d.py`** — a Matplotlib `Poly3DCollection` PNG: wall and roof
  faces shaded by absolute elevation and building height.

The samples are the OGC's own example models, shipped inside the CityGML
schema archive: `Building_LOD2-EPSG25832.gml` (7 surfaces, solids declared
by `xlink:href` into the semantic surfaces), `Building_LOD1-EPSG25832.gml`
(smallest full model), and `Building_and_garage_LOD2-EPSG25832.gml` (a
`Building` with a nested `BuildingPart`, plus an `OrientableSurface`
wrapper).

## Setup

```sh
uv run python examples/citygml/download_sources.py
uv sync --group examples-matplotlib   # only needed for plot_city3d.py
```

Sources are pinned by SHA-256 in [`sources.toml`](sources.toml) and
installed under the gitignored `examples/citygml/.cache/`. Repeating the
download command verifies the cache without network access.

## Run

```sh
# default sample, default output under examples/citygml/out/
uv run python examples/citygml/plan_svg.py
uv run python examples/citygml/plot_city3d.py

# explicit sample and destination
uv run python examples/citygml/plan_svg.py \
    examples/citygml/.cache/citygml-schemas/citygml/examples/2.0/building/Building_LOD1-EPSG25832.gml \
    -o examples/citygml/out/lod1-plan.svg
uv run python examples/citygml/plot_city3d.py <model.gml> -o out/city3d.png
```

## Serve

The family also includes a local FastAPI use for curated rendering and
submitted XML validation. It reuses this family's pinned schema closure,
OGC samples, plan renderer, and 3D plot:

```sh
uv run python examples/citygml/download_sources.py
uv sync --group examples-fastapi --group examples-matplotlib
uv run uvicorn examples.citygml.server:app --port 8000
```

Open <http://127.0.0.1:8000/> for the sample browser. `/render/lod1` and
`/render/lod2` return SVG plans; add `?format=png` for the 3D plot.
Submit a CityGML document for diagnostics and a building count:

```sh
curl --data-binary @model.gml -H 'Content-Type: application/xml' \
    http://127.0.0.1:8000/validate
```

The published CityGML samples carry their documented schema and instance
findings, so validation reports them while curated rendering still works.
This is a local example, not a deployment recipe for untrusted uploads.

From Python, compile once per process and reuse the schema:

```python
from pathlib import Path

from examples.citygml.plan_svg import collect_buildings, render_plan
from examples.citygml.plot_city3d import plot_city_3d
from examples.citygml.common.schemas import compile_schema

document = compile_schema().parse(Path("model.gml"))
buildings = collect_buildings(document)  # [{id, rings: [[(x, y), ...]], measured_height}]
svg = render_plan(document, width=900)  # deterministic bytes
png = plot_city_3d(document, Path("out/city3d.png"))
```

## What the walk understands

- `lod2Solid` first, then `lod1Solid`: `Solid → exterior → CompositeSurface`
  or `MultiSurface → surfaceMember → Polygon|Triangle → exterior →
  LinearRing → posList` (`srsDimension="3"`, space-separated ordinates).
- `surfaceMember xlink:href="#id"` references, resolved against a
  document-wide `gml:id` index (the OGC LOD2 solids are written this way).
- `OrientableSurface → baseSurface` wrappers (the garage example).
- Semantic `boundedBy` `WallSurface`/`RoofSurface`/`GroundSurface` →
  `lod2MultiSurface` when a building has no solid.
- Nested `BuildingPart`s are listed as their own footprint with their own
  `measuredHeight` instead of being merged into the parent.
- Only the outer ring of each polygon is kept; rings are closed before
  rendering. `bldg:measuredHeight` is read from the element text; its
  `uom="#m"` is a dangling local fragment reference in the official files
  and is deliberately not resolved.

The plan view is north-up (y inverted for SVG), scaled to the requested
width, and deterministic: buildings are sorted by `gml:id`, coordinates are
formatted with fixed precision, and nothing timestamped is emitted.

## Known findings on the stock schema

`compile_schema()` composes the real CityGML 2.0 closure and the
GML 3.1.1 closure it imports. Do **not** call `require_valid()`: the
official files themselves trip 22 error-severity findings:

- 21 × `particle-restriction` — GML 3.1.1 derives geometry types by
  extension inside restrictions of `AbstractGeometryType` (and similar
  `metaDataProperty`/`description` sequence mismatches). Every strict
  validator reports these; they are inherent to the published files.
- 1 × `unknown-attributeRef` — `gml:uom` referenced from
  `CoordinateSystemAxisType` cannot be resolved in GML 3.1.1.

Parsing the example instances adds exactly 3 instance-phase errors, all
from the abstract `gml:_Feature` particle that composition cannot bind:

- `order` on `FeaturePropertyType` (`_Feature` expected in a different
  position),
- `unexpected-element` for `dem:ReliefFeature`,
- `occurrence-min` for the missing `gml:_Feature`.

The trees still bind `cityObjectMember → Building`, the solids, and the
semantic surfaces, which is all both renderers need. The tests assert the
report stays at or below 30 errors and that those codes remain present, so
a future improvement to composition support shows up as a deliberate
baseline update rather than silent drift.

## Limitations

- LOD1/LOD2 `gml:Solid` geometry only; LOD3/LOD4 solids, `lod2MultiSurface`
  buildings without solids beyond the `boundedBy` fallback, and ADE
  geometry are not traversed.
- Footprint outer rings only: interior rings (courtyards) are ignored.
- No reprojection. The samples are ETRS89/UTM32N (EPSG:25832) with
  absolute heights; the plan treats the ordinates as a planar metre grid.
- No appearance/textures, no `dem:ReliefFeature` terrain rendering, no
  glTF/Cesium export. The 3D view is a static PNG; a browser viewer is out
  of scope for a server-side example.
- Building parts are separate entries, so a parent's `measuredHeight` and
  its parts' heights are not combined into one number.
- CityGML 1.0 and 3.0 encodings are not supported (different namespaces
  and different geometry encodings).

## Tests

```sh
uv run pytest tests/test_citygml_examples.py tests/test_citygml_server.py -q
```

The synthetic subset tests (ring extraction, `xlink`/`OrientableSurface`
following, ring closure, height shading, deterministic SVG, PNG output)
always run. The real-model tests skip when `.cache/` is absent and name the
download command in the skip reason.

## Attribution

The CityGML 2.0 schemas and example models are published by the Open
Geospatial Consortium (© 2012 OGC); licenses and verbatim notices are
recorded in [`SOURCES.md`](SOURCES.md).
