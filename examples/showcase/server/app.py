"""Local-only HTTP showcase; schemas and optional renderers load on demand."""

from __future__ import annotations

from html import escape
from importlib import import_module
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import quote
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from examples.showcase.common.sources import CACHE_ROOT, SETUP_HINT
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask

SHOWCASE_ROOT = Path(__file__).resolve().parents[1]
MAX_XML_BYTES = 8 * 1024 * 1024


def _lazy(module: str, name: str):
    def invoke(*args, **kwargs):
        function = getattr(import_module(f"examples.showcase.{module}"), name)
        return function(*args, **kwargs)

    return invoke


def _default_registry() -> dict:
    """Resolve the curated local paths once, without importing any renderer."""
    svg_type = "image/svg+xml"

    def sample(path, module, name, media_type=svg_type, **extra):
        return {"path": path, "renderer": _lazy(module, name), "media_type": media_type, **extra}

    city_png = {
        "png": {
            "renderer": _lazy("citygml.plot_city3d", "plot_city_3d"),
            "media_type": "image/png",
            "suffix": ".png",
        }
    }
    road_png = {
        "png": {
            "renderer": _lazy("opendrive.plot_profiles", "plot_profiles"),
            "media_type": "image/png",
            "suffix": ".png",
        }
    }
    invoice_name = "01.01a-INVOICE_ubl.xml"
    invoice = next(
        iter(sorted((CACHE_ROOT / "ubl-data").rglob(invoice_name))),
        CACHE_ROOT / "ubl-data" / invoice_name,
    )
    city_root = CACHE_ROOT / "citygml-schemas/citygml/examples/2.0/building"
    return {
        "citygml": {
            "schema_domain": "citygml",
            "facts": lambda doc: {
                "buildings": len(_lazy("citygml.plan_svg", "collect_buildings")(doc))
            },
            "samples": {
                f"lod{lod}": sample(
                    city_root / f"Building_LOD{lod}-EPSG25832.gml",
                    "citygml.plan_svg",
                    "render_plan",
                    formats=city_png,
                    note=f"OGC LOD{lod} building; known stock schema findings",
                )
                for lod in (1, 2)
            },
        },
        "opendrive": {
            "schema_domain": "opendrive",
            "facts": lambda doc: {"roads": len(_lazy("opendrive.render_map", "road_paths")(doc))},
            "samples": {
                name: sample(
                    CACHE_ROOT / "opendrive-data" / f"{name}.xodr",
                    "opendrive.render_map",
                    "render_map",
                    formats=road_png,
                )
                for name in ("Town01", "Town02", "soderleden", "curves_elevation")
            },
        },
        "bpmn": {
            "schema_domain": "bpmn",
            "facts": _bpmn_facts,
            "samples": {
                name: sample(
                    CACHE_ROOT / "bpmn-data" / f"{name}.bpmn", "bpmn.render_svg", "render_svg"
                )
                for name in ("starter", "simple", "sequence-flows", "nested-subprocesses")
            },
        },
        "ubl": {
            "schema_domain": "ubl",
            "facts": lambda doc: {
                "payable": _lazy("invoice.render_html", "invoice_facts")(doc)["totals"]["payable"]
            },
            "samples": {
                "01.01a-INVOICE_ubl": sample(
                    invoice,
                    "invoice.render_html",
                    "render_html",
                    "text/html",
                    formats={
                        "pdf": {
                            "renderer": _lazy("invoice.render_pdf", "render_pdf"),
                            "media_type": "application/pdf",
                            "suffix": ".pdf",
                        }
                    },
                    note="XRechnung invoice; XSD validation is not Schematron validation",
                )
            },
        },
        "score": {
            "schema_domain": "musicxml",
            "facts": lambda doc: {"notes": len(_lazy("score.synthesize", "notes")(doc))},
            "samples": {
                "chorale": sample(
                    SHOWCASE_ROOT.parent / "musicxml/instance.xml",
                    "score.synthesize",
                    "synthesize",
                    "audio/wav",
                    suffix=".wav",
                    formats={
                        "svg": {
                            "renderer": _lazy("score.render_preview", "render_preview"),
                            "media_type": svg_type,
                        }
                    },
                    note="Bach BWV 66.6; public-domain score",
                )
            },
        },
    }


def _bpmn_facts(document) -> dict:
    return {
        "shapes": len(_lazy("bpmn.render_svg", "di_shapes")(document)),
        "edges": len(_lazy("bpmn.render_svg", "di_edges")(document)),
    }


def _variant(domain: str, data: bytes) -> str | None:
    if domain != "opendrive":
        return None
    root = DefusedET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    header = root.find("header")
    if header is None or header.get("revMajor") != "1":
        raise ValueError("OpenDRIVE requires a header with revMajor=1")
    minor = header.get("revMinor")
    variants = {"4": "1.4", "5": "1.5", "6": "1.7", "7": "1.7"}
    if minor not in variants:
        raise ValueError("Supported OpenDRIVE revisions: 1.4, 1.5, 1.6, 1.7")
    return variants[minor]


def _diagnostic(exc: Exception) -> dict:
    return {
        "valid": False,
        "issues": [
            {
                "severity": "error",
                "code": "xml-input",
                "message": str(exc),
                "element": None,
                "phase": "instance",
            }
        ],
    }


def _artifact(document, spec: dict) -> Response:
    if "suffix" not in spec:
        return Response(spec["renderer"](document), media_type=spec["media_type"])
    temporary = TemporaryDirectory(prefix="pyxsd-showcase-")
    try:
        destination = Path(temporary.name) / ("render" + spec["suffix"])
        spec["renderer"](document, destination)
        return FileResponse(
            destination, media_type=spec["media_type"], background=BackgroundTask(temporary.cleanup)
        )
    except Exception:
        temporary.cleanup()
        raise


def _endpoint(domain: str, sample: str) -> str:
    return f"/render/{quote(domain, safe='')}/{quote(sample, safe='')}"


def _inventory(registry: dict) -> list[dict]:
    return [
        {
            "domain": domain,
            "sample": sample,
            "endpoint": _endpoint(domain, sample),
            "media_type": spec["media_type"],
            "note": spec.get("note", ""),
        }
        for domain, binding in sorted(registry.items())
        for sample, spec in sorted(binding["samples"].items())
    ]


def _schema(application: FastAPI, domain: str, variant: str | None = None):
    key = domain, variant
    if key not in application.state.schemas:
        binding = application.state.registry[domain]
        if "schema" in binding:
            from pyxsd import Schema
            from pyxsd.binding import ParseModes

            path = Path(binding["schema"])
            if not path.is_file():
                raise FileNotFoundError(path)
            schema = Schema.compile(path, mode=ParseModes.LAX)
        else:
            from examples.showcase.common.schemas import compile_schema

            # The spine's process cache must not share mutable parsers across apps.
            schema = compile_schema(binding["schema_domain"], variant, fresh=True)
        application.state.schemas[key] = schema
    return application.state.schemas[key]


def _unavailable(exc: Exception) -> HTTPException:
    return HTTPException(503, f"{exc}; run: {SETUP_HINT}")


def create_app(registry: dict | None = None) -> FastAPI:
    """Build an isolated app; injected domains supply schema, samples, and facts."""
    application = FastAPI(title="pyxsd rendering showcase")
    application.state.registry = _default_registry() if registry is None else registry
    application.state.schemas = {}
    # Schema.parse is neither concurrent nor reentrant. Keep compile, parse,
    # report inspection and rendering together; handlers run in the threadpool.
    application.state.schema_lock = Lock()

    @application.get("/", response_class=HTMLResponse)
    def landing() -> str:
        sections = []
        for domain in sorted(application.state.registry):
            links = "".join(
                f'<li><a href="{escape(item["endpoint"])}">{escape(item["sample"])}</a>'
                f" <small>{escape(item['note'])}</small></li>"
                for item in _inventory(application.state.registry)
                if item["domain"] == domain
            )
            command = (
                "curl --data-binary @document.xml -H 'Content-Type: application/xml' "
                f"http://127.0.0.1:8000/validate/{quote(domain, safe='')}"
            )
            sections.append(
                f"<section><h2>{escape(domain)}</h2><ul>{links}</ul>"
                f"<p>POST your XML:</p><pre>{escape(command)}</pre></section>"
            )
        return (
            '<!DOCTYPE html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>pyxsd rendering showcase</title><style>"
            "body{font:16px/1.6 system-ui,sans-serif;color:#172b45;background:#f4f7fb;"
            "max-width:960px;margin:40px auto;padding:0 20px}h1,h2{line-height:1.2}"
            "section{background:white;padding:20px;margin:20px 0;border-radius:10px}"
            "a{color:#165ba6}small{color:#52647a}pre{overflow:auto;background:#edf2f8;"
            "padding:12px;border-radius:6px}</style><h1>XML in, artifacts out</h1>"
            "<p>Server-side rendering replaces browser XSLT: validate, bind, draw.</p>"
            + "".join(sections)
            + "<footer>Local examples only. Do not expose this server publicly.</footer></html>"
        )

    @application.get("/api/samples")
    def samples() -> list[dict]:
        return _inventory(application.state.registry)

    @application.get("/render/{domain}/{sample}")
    def render(domain: str, sample: str, format: str | None = None) -> Response:
        from pyxsd import PyXSDError

        binding = application.state.registry.get(domain)
        if binding is None or sample not in binding["samples"]:
            raise HTTPException(404, "Unknown sample")
        spec = binding["samples"][sample]
        output = spec
        if format is not None:
            output = spec.get("formats", {}).get(format)
            if output is None:
                raise HTTPException(400, "Unknown output format")
        try:
            with Path(spec["path"]).open("rb") as source:
                data = source.read(MAX_XML_BYTES + 1)
            if len(data) > MAX_XML_BYTES:
                raise HTTPException(413, "XML exceeds the 8 MB limit")
            with application.state.schema_lock:
                document = _schema(application, domain, _variant(domain, data)).parse(BytesIO(data))
                return _artifact(document, output)
        except FileNotFoundError as exc:
            raise _unavailable(exc) from exc
        except ImportError as exc:
            raise HTTPException(
                503, "Install the examples-matplotlib/examples-reportlab group"
            ) from exc
        except (PyXSDError, ValueError, ParseError, DefusedXmlException) as exc:
            return JSONResponse(_diagnostic(exc), status_code=422)

    return application


app = create_app()
