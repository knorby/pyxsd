# Browse XML, render on the server

One local FastAPI app mounts the five example-family workflows: CityGML building
plans, OpenDRIVE road maps, BPMN diagrams, UBL invoices, and a MusicXML
chorale you can hear. The renderers consume pyxsd's bound documents rather
than raw XML strings. **Server-side rendering replaces browser XSLT**:
the browser receives SVG, HTML, PDF, PNG, or WAV, not a stylesheet that
depends on a browser XSLT engine.

## Run

From the repository root:

```sh
uv run python examples/citygml/download_sources.py   # and the other families
uv run --group examples-fastapi uvicorn examples.server.app:app --port 8000
```

Open <http://127.0.0.1:8000/> for sample links and copyable upload commands.
The landing page and JSON inventory work without the cache; rendering or
validation that needs missing sources returns **503** with the download
command. No import or request downloads anything.

The module path above was verified by starting uvicorn on port 8000 and
requesting `/` (200 HTML) and `/api/samples` (200 JSON, 12 samples).
Verification used `UV_NO_SYNC=1` to preserve the already-installed shared
test environment; normal `uv run --group examples-fastapi` installs the
server group automatically.

For the optional PNG and PDF outputs, include their groups in the launch
command so uv retains those dependencies:

```sh
uv run --group examples-fastapi --group examples-matplotlib --group examples-reportlab \
    uvicorn examples.server.app:app --port 8000
```

## Endpoints

| Request | Response |
| --- | --- |
| `GET /` | Self-contained `text/html` sample browser |
| `GET /api/samples` | `application/json`: domain, sample, endpoint, media type, note |
| `GET /render/citygml/lod1` or `lod2` | `image/svg+xml`; add `?format=png` for `image/png` 3D buildings |
| `GET /render/opendrive/Town01`, `Town02`, `soderleden`, or `curves_elevation` | `image/svg+xml`; add `?format=png` for `image/png` profiles |
| `GET /render/bpmn/starter`, `simple`, `sequence-flows`, or `nested-subprocesses` | `image/svg+xml` |
| `GET /render/ubl/01.01a-INVOICE_ubl` | `text/html`; add `?format=pdf` for `application/pdf` |
| `GET /render/score/chorale` | `audio/wav`; add `?format=svg` for `image/svg+xml` preview |
| `POST /validate/{domain}` | `application/json` diagnostics and, if valid, domain facts |

Validation domains are `citygml`, `opendrive`, `bpmn`, `ubl`, and `score`.
The last maps to the spine's `musicxml` schema. Send raw XML bytes, not
JSON or a multipart upload:

```sh
curl --data-binary @document.xml -H 'Content-Type: application/xml' \
    http://127.0.0.1:8000/validate/ubl
```

Malformed XML, parser-policy denials, and schema-invalid documents return
**200** with `valid: false` and an `issues` list. Each issue has `severity`,
`code`, `message`, `element`, and `phase`. Valid documents additionally
have `facts`: building count, road count, shape/edge counts, payable amount
(the exact lexical string), or sounding-note count. This is XSD
validation, **not EN 16931/Schematron invoice compliance**.

Unknown domains/samples return **404**; unsupported output formats return
**400**; bodies over **8 MiB** return **413**, including streamed bodies
without a content length. Missing caches or optional renderer packages
return **503**. A curated source that cannot be parsed or rendered returns
**422** diagnostics. CityGML's published schema and instances have
[known findings](../citygml/README.md): the curated geometry can still be
rendered, but validation reports those errors rather than hiding them.

## App state and version selection

`create_app(registry=None)` creates isolated schema and registry state.
Schemas compile lazily once per app and variant, using
`compile_schema(..., fresh=True)` to avoid sharing the spine's mutable
process-cached parser between apps. A lock covers compilation, parsing,
report inspection, and rendering; blocking work runs in the threadpool.
The injectable registry supports offline tests with a tiny local XSD,
sample paths, renderer callables, and a facts adapter.

OpenDRIVE has no namespace version signal. A bounded, defused XML pre-parse
reads `<header revMajor="1" revMinor="…">`: minor 4 selects schema `1.4`,
5 selects `1.5`, and 6 or 7 selects `1.7`, matching the road CLI's
compatibility rule. Missing or unsupported revisions become diagnostics;
the server does not guess from `version` or instance schema-location hints.
The UBL sample path is found once per app by a sorted search under
`.cache/ubl-data`; no request scans arbitrary filesystem paths.

File renderers write into a private temporary directory. `FileResponse`
sends the artifact, then a background task removes the directory. Failed
renderers clean up immediately. No generated artifact is committed.

## Security: examples, not production

**Do not expose this server publicly.** It is a local demonstration, not
an authenticated upload service or a resource-isolated execution worker.
Untrusted-input deployments need the full
[XML security guidance](../../../docs/xml-security.md), current parser
security updates, deployment limits, and a deliberate threat model.

POST validation uses `XmlParserPolicy.strict()` with explicit byte,
depth (128), and element (200,000) budgets. DTDs and entity declarations
are refused. Consequently the committed MusicXML file's legacy external
DOCTYPE must be removed before posting it; the trusted curated rendering
path accepts that original file without fetching its external DTD.
These parser limits do not bound all schema processing CPU or memory.
The render routes accept only curated IDs, never caller-supplied paths,
schemas, or renderer names. Registry entries are trusted application code,
not user configuration. Landing-page values and renderer text are escaped.

## Tests

```sh
uv run --group examples-fastapi --with httpx pytest tests/test_render_server.py -q
uvx ruff@0.16.6 check examples/server tests/test_render_server.py
uvx ruff@0.16.6 format --check examples/server tests/test_render_server.py
```

The test module skips when FastAPI or a supported TestClient HTTP client
is absent (`httpx` on older Starlette, `httpx2` on newer releases). Offline
tests need no downloads. Real-schema smoke tests skip with the setup
command when their caches are missing; optional PNG/PDF tests also skip
without their rendering dependencies. Sources, terms, and attribution
remain in [`SOURCES.md`](SOURCES.md).
