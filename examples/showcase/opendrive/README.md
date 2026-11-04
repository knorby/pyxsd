# OpenDRIVE: render road networks from XML

ASAM OpenDRIVE is the XML exchange format behind most driving simulators:
a road network is a set of roads, each with a piecewise reference line
(`line`, `arc`, `spiral`, `poly3`, `paramPoly3`), a lane cross-section,
and cubic elevation/superelevation profiles. This workflow binds real
`.xodr` files with pyxsd and turns them into two artifacts:

- an **SVG lane map** with every reference line and lane boundary
  (stdlib only, deterministic bytes), and
- a **Matplotlib profile PNG** with elevation per road, plus
  superelevation when the file carries it.

No geometry library is involved: sampling and lane offsetting are plain
Python, so the example also shows how much of a CAD/GIS pipeline is just
"parse, evaluate polynomials, draw".

## Setup

From a pyxsd checkout:

```sh
uv run python examples/showcase/download_sources.py \
    --only opendrive-schema --only opendrive-data
```

That verifies and installs the pinned XSDs and sample networks under
`examples/showcase/.cache/` (gitignored). The matplotlib extra is only
needed for the profile plot:

```sh
uv sync --group examples-matplotlib
```

## Run

Render a map (the default output is `examples/showcase/out/<name>.svg`):

```sh
uv run python examples/showcase/opendrive/render_map.py \
    examples/showcase/.cache/opendrive-data/Town01.xodr

uv run python examples/showcase/opendrive/render_map.py \
    examples/showcase/.cache/opendrive-data/soderleden.xodr \
    -o /tmp/soderleden.svg
```

Plot the profiles (default output is
`examples/showcase/out/<name>-profiles.png`):

```sh
uv run python examples/showcase/opendrive/plot_profiles.py \
    examples/showcase/.cache/opendrive-data/curves_elevation.xodr
```

Pinned samples:

| File | Version | Roads | What it exercises |
| --- | --- | --- | --- |
| `Town01.xodr` | 1.4 | 98 | CARLA town: arcs, 12 junctions, 176 lane sections |
| `Town02.xodr` | 1.4 | 68 | second CARLA town |
| `curves_elevation.xodr` | 1.4 | 1 | 7 spirals and a 16-record elevation profile |
| `fabriksgatan.xodr` | 1.4 | 16 | `paramPoly3` geometry and `laneOffset` |
| `soderleden.xodr` | 1.7 | 5 | 1.7 schema set, highway interchange |

## Interfaces

The FastAPI showcase server imports these functions directly:

```python
# render_map.py
def sample_geometry(geom: dict, length: float, step: float = 1.0) -> list[tuple[float, float]]
def road_paths(document) -> list[dict]   # road_id, length, centerline, lanes
def render_map(document, *, width: int = 1000) -> str

# plot_profiles.py
def plot_profiles(document, dest: Path) -> Path
```

`sample_geometry` takes a JSON-able dict holding the geometry element's
attributes plus a `primitive` key naming the child (`"line"`, `"arc"`,
`"spiral"`, `"poly3"`, `"paramPoly3"`); attributes may be written with or
without `to_dict`'s `@` prefix. `road_paths` returns lane *outer
boundaries* (cumulative widths along the local left normal), and
`render_map` labels each road id at its first centerline point.

## How it works

1. `compile_schema("opendrive")` compiles the pinned 1.4H XSD in LAX mode.
   OpenDRIVE instances are unqualified: there is no `xmlns` on
   `<OpenDRIVE>`, and the official XSDs declare no `targetNamespace`.
2. `document.to_dict()` gives a stable, JSON-shaped tree. Child-element
   properties on the generated classes can return `None` for
   reference-heavy content models, so the renderer walks the dict rather
   than relying on per-element descriptors.
3. Each geometry is sampled in its local frame (`u` along, `v` left)
   from the declared `x`, `y`, `hdg` and `length`. `line` and `arc` use
   closed-form positions; `spiral` integrates heading numerically with
   Simpson's rule; `poly3` and `paramPoly3` evaluate their polynomials
   (honouring `pRange="normalized"` by scaling `p` to `s/length`).
4. At every sample, each lane edge is offset along the local normal by
   the cumulative lane widths. Width and `laneSection` records are
   piecewise in `s`; the road-level `laneOffset` cubic bends the whole
   cross-section and is applied to every lane.
5. The SVG fits all centerline and lane points into the requested width
   with a uniform scale and a y-axis flip (SVG y grows downwards).

## Version picking

The instance header is the only reliable version signal:

```xml
<header revMajor="1" revMinor="4"/>
```

The pinned `sources.toml` entries expose variants `"1.4"`, `"1.5"`, and
`"1.7"`. The CLIs map `revMinor` 4 to 1.4, 5 to 1.5, and 6 or later to
1.7 (1.5-1.7 only add elements and attributes, so 1.6 documents validate
against the 1.7 schema). Both `compile_schema("opendrive")` and the CLI's
default select the single-file 1.4H schema; pass `"1.7"` explicitly for
`soderleden.xodr`.

Do not use the `version` attribute: real files carry `"1.00"`, `"1"`, or
nothing at all. None of the samples carries
`xsi:schemaLocation`/`xsi:noNamespaceSchemaLocation`, so the schema
cannot be discovered from the document.

The 1.6.1/1.7 schema sets include each other cyclically; pyxsd emits a
harmless `compose-cycle` **warning** for that include and still exits
cleanly. Warnings are not validation errors.

## Limitations

- **Junction connectors are not drawn.** Roads are rendered independently;
  junction `connection` elements (how lanes link across intersections)
  are ignored, so a map shows the road skeleton and lane boundaries but
  not turn paths through junctions.
- Lane *borders*, road marks, materials, heights, signals, objects,
  bridges, and tunnels are not rendered.
- Elevation is not used in the 2D map; it appears only in the profile
  PNG. Profiles are drawn per road because `s` restarts at each road.
- Geometry is sampled at a fixed 1 m step, so very tight curves become
  visibly polygonal. `spiral` positions come from numerical integration,
  not Fresnel functions.
- The pinned schemas are the unqualified dialect. Files using ASAM's
  namespaced test dialect (`xmlns="...opendrive_schema"`) will not
  validate against them.
- Versions 1.8/1.9 are not pinned: those schemas are XSD 1.1 and are
  outside this example's version-picking scope.
- `laneOffset` is handled, but overlapping/extra lane records that some
  authoring tools emit beyond the spec are not reconciled.

## Sources and attribution

Every schema and `.xodr` file is downloaded at runtime, digest-verified,
and never committed. Provenance, revisions, and verbatim license evidence
(CARLA MIT, esmini MPL-2.0, ASAM schema terms) live in
[`../SOURCES.md`](../SOURCES.md); the pinned URLs and SHA-256 digests are
in [`../sources.toml`](../sources.toml).
