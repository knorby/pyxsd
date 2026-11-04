"""Local-only HTTP showcase; schemas and optional renderers load on demand."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from examples.showcase.common.sources import SETUP_HINT
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response


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
    application.state.registry = {} if registry is None else registry
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
    def render(domain: str, sample: str) -> Response:
        binding = application.state.registry.get(domain)
        if binding is None or sample not in binding["samples"]:
            raise HTTPException(404, "Unknown sample")
        spec = binding["samples"][sample]
        try:
            data = Path(spec["path"]).read_bytes()
            with application.state.schema_lock:
                document = _schema(application, domain).parse(BytesIO(data))
                return Response(spec["renderer"](document), media_type=spec["media_type"])
        except FileNotFoundError as exc:
            raise _unavailable(exc) from exc

    return application


app = create_app()
