"""Serve MusicXML validation, WAV synthesis, and previews from one app."""

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
from .common.sources import FAMILY_ROOT, SETUP_HINT
from .render_preview import render_preview
from .synthesize import notes

MAX_XML_BYTES = 8 * 1024 * 1024
WAV_TYPE = "audio/wav"
SVG_TYPE = "image/svg+xml"


def _synthesize(document: Any, destination: Path) -> Path:
    from .synthesize import synthesize

    return synthesize(document, destination)


def _default_samples() -> dict[str, dict]:
    return {
        "chorale": {
            "path": FAMILY_ROOT / "instance.xml",
            "renderer": _synthesize,
            "media_type": WAV_TYPE,
            "suffix": ".wav",
            "note": "Bach BWV 66.6; public-domain score",
            "formats": {
                "svg": {
                    "renderer": render_preview,
                    "media_type": SVG_TYPE,
                }
            },
        }
    }


def _fresh_schema(variant: str | None):
    return compile_schema(variant, fresh=True)


def _facts(document: Any) -> dict:
    return {"notes": len(notes(document))}


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


def _input_error() -> dict:
    return {
        "valid": False,
        "issues": [
            {
                "severity": "error",
                "code": "xml-input",
                "message": "the XML input could not be processed",
                "element": None,
                "phase": "instance",
            }
        ],
    }


def _unavailable() -> HTTPException:
    return HTTPException(503, f"MusicXML example sources are not installed; {SETUP_HINT}")


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
    temporary = TemporaryDirectory(prefix="musicxml-example-")
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
    """Build a MusicXML-only app; injected samples/factory isolate offline tests."""
    application = FastAPI(title="MusicXML example server")
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
            "curl --data-binary @score.musicxml -H 'Content-Type: application/xml' "
            "http://127.0.0.1:8000/validate"
        )
        return (
            '<!DOCTYPE html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>MusicXML example server</title><style>"
            "body{font:16px/1.6 system-ui,sans-serif;color:#172b45;background:#f4f7fb;"
            "max-width:960px;margin:40px auto;padding:0 20px}h1,h2{line-height:1.2}"
            "section{background:white;padding:20px;margin:20px 0;border-radius:10px}"
            "a{color:#165ba6}small{color:#52647a}pre{overflow:auto;background:#edf2f8;"
            "padding:12px;border-radius:6px}</style><h1>MusicXML scores</h1>"
            "<p>Validate a score, synthesize its sounding notes, or draw a staff "
            "preview.</p>"
            f"<section><h2>Samples</h2><ul>{links}</ul><p>POST a score:</p>"
            f"<pre>{escape(command)}</pre></section>"
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
        except (PyXSDError, ValueError, LookupError, ParseError, DefusedXmlException):
            return JSONResponse(_input_error(), status_code=422)

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
