"""Serve XRechnung invoice validation, HTML, and PDF from a family-local app."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any
from urllib.parse import quote
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .common.schemas import compile_schema
from .common.sources import CACHE_ROOT, SETUP_HINT
from .render_html import invoice_facts, render_html

MAX_XML_BYTES = 8 * 1024 * 1024
HTML_TYPE = "text/html"
PDF_TYPE = "application/pdf"
INVOICE_NAME = "01.01a-INVOICE_ubl.xml"


def _render_pdf(document: Any, destination: Path) -> Path:
    from .render_pdf import render_pdf

    return render_pdf(document, destination)


def _default_samples() -> dict[str, dict]:
    data_root = CACHE_ROOT / "ubl-data"
    invoice = next(iter(sorted(data_root.rglob(INVOICE_NAME))), data_root / INVOICE_NAME)
    return {
        "01.01a-INVOICE_ubl": {
            "path": invoice,
            "renderer": render_html,
            "media_type": HTML_TYPE,
            "note": "XRechnung invoice; XSD validation is not Schematron validation",
            "formats": {
                "pdf": {
                    "renderer": _render_pdf,
                    "media_type": PDF_TYPE,
                    "suffix": ".pdf",
                }
            },
        }
    }


def _fresh_schema(variant: str | None):
    return compile_schema(variant, fresh=True)


def _facts(document: Any) -> dict:
    return {"payable": invoice_facts(document)["totals"]["payable"]}


def _issues(report: Any) -> list[dict]:
    return [
        {
            "severity": issue.severity.value,
            "code": issue.code,
            "message": issue.message,
            "element": issue.element,
            "phase": issue.phase,
        }
        for issue in report.issues
    ]


def _diagnostic(exc: Exception) -> dict:
    from pyxsd import PyXSDError

    report = getattr(exc, "report", None)
    if report is not None and report.issues:
        return {"valid": False, "issues": _issues(report)}
    if isinstance(exc, PyXSDError):
        message = str(exc)
    else:
        message = f"{type(exc).__name__}: the XML input could not be processed"
    return {
        "valid": False,
        "issues": [
            {
                "severity": "error",
                "code": "xml-input",
                "message": message,
                "element": None,
                "phase": "instance",
            }
        ],
    }


def _unavailable() -> HTTPException:
    return HTTPException(503, f"XRechnung example sources are not installed; {SETUP_HINT}")


def _schema(application: FastAPI, variant: str | None = None):
    key = variant
    if key not in application.state.schemas:
        application.state.schemas[key] = application.state.schema_factory(variant)
    return application.state.schemas[key]


def _validate(application: FastAPI, data: bytes) -> dict:
    from pyxsd import PyXSDError, XmlLimits, XmlParserPolicy

    try:
        with application.state.schema_lock:
            document = _schema(application).parse(
                BytesIO(data),
                xml_policy=XmlParserPolicy.strict(),
                xml_limits=XmlLimits(max_bytes=MAX_XML_BYTES, max_depth=128, max_elements=200_000),
            )
            result = {"valid": not document.report.has_errors, "issues": _issues(document.report)}
            if result["valid"]:
                result["facts"] = application.state.facts(document)
            return result
    except FileNotFoundError as exc:
        raise _unavailable() from exc
    except (PyXSDError, ValueError, LookupError, ParseError, DefusedXmlException) as exc:
        return _diagnostic(exc)


def _artifact(document: Any, spec: dict) -> Response:
    if "suffix" not in spec:
        return Response(spec["renderer"](document), media_type=spec["media_type"])
    temporary = TemporaryDirectory(prefix="xrechnung-example-")
    try:
        destination = Path(temporary.name) / ("render" + spec["suffix"])
        spec["renderer"](document, destination)
        return FileResponse(
            destination,
            media_type=spec["media_type"],
            background=BackgroundTask(temporary.cleanup),
        )
    except Exception:
        temporary.cleanup()
        raise


def _endpoint(sample: str) -> str:
    return f"/render/{quote(sample, safe='')}"


def _inventory(samples: dict[str, dict]) -> list[dict]:
    return [
        {
            "sample": sample,
            "endpoint": _endpoint(sample),
            "media_type": spec["media_type"],
            "note": spec.get("note", ""),
        }
        for sample, spec in sorted(samples.items())
    ]


def create_app(
    *,
    samples: dict[str, dict] | None = None,
    schema_factory: Callable[[str | None], Any] | None = None,
) -> FastAPI:
    """Build an XRechnung-only app; injected samples/factory isolate offline tests."""
    application = FastAPI(title="XRechnung example server")
    application.state.samples = _default_samples() if samples is None else samples
    application.state.schema_factory = schema_factory or _fresh_schema
    application.state.facts = _facts
    application.state.schemas = {}
    application.state.schema_lock = Lock()

    @application.get("/", response_class=HTMLResponse)
    def landing() -> str:
        links = "".join(
            f'<li><a href="{escape(item["endpoint"])}">{escape(item["sample"])}</a>'
            f" <small>{escape(item['note'])}</small></li>"
            for item in _inventory(application.state.samples)
        )
        command = (
            "curl --data-binary @invoice.xml -H 'Content-Type: application/xml' "
            "http://127.0.0.1:8000/validate"
        )
        return (
            '<!DOCTYPE html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>XRechnung example server</title><style>"
            "body{font:16px/1.6 system-ui,sans-serif;color:#172b45;background:#f4f7fb;"
            "max-width:960px;margin:40px auto;padding:0 20px}h1,h2{line-height:1.2}"
            "section{background:white;padding:20px;margin:20px 0;border-radius:10px}"
            "a{color:#165ba6}small{color:#52647a}pre{overflow:auto;background:#edf2f8;"
            "padding:12px;border-radius:6px}</style><h1>XRechnung invoices</h1>"
            "<p>Render a UBL invoice as HTML or PDF, or validate its XML against "
            "the UBL 2.3 schema.</p>"
            f"<section><h2>Samples</h2><ul>{links}</ul><p>POST an invoice:</p>"
            f"<pre>{escape(command)}</pre><p>XSD validation does not establish "
            "EN 16931 or Schematron business-rule conformance.</p></section>"
            "<footer>Local examples only. Do not expose this server publicly.</footer></html>"
        )

    @application.get("/api/samples")
    def inventory() -> list[dict]:
        return _inventory(application.state.samples)

    @application.get("/render/{sample}")
    def render(sample: str, format: str | None = None) -> Response:
        from pyxsd import PyXSDError

        spec = application.state.samples.get(sample)
        if spec is None:
            raise HTTPException(404, "Unknown sample")
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
                document = _schema(application).parse(BytesIO(data))
                return _artifact(document, output)
        except FileNotFoundError as exc:
            raise _unavailable() from exc
        except ImportError as exc:
            raise HTTPException(503, "Install the examples-reportlab group") from exc
        except (PyXSDError, ValueError, LookupError, ParseError, DefusedXmlException) as exc:
            return JSONResponse(_diagnostic(exc), status_code=422)

    @application.post("/validate")
    async def validate(request: Request) -> dict:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_XML_BYTES:
                raise HTTPException(413, "XML exceeds the 8 MB limit")
            data.extend(chunk)
        return await run_in_threadpool(_validate, application, bytes(data))

    return application


app = create_app()
