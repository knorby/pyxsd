"""Offline HTTP contracts, with optional pinned-corpus smoke tests."""

from importlib.util import find_spec
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
# New Starlette uses httpx2; older supported installations use httpx.
pytest.importorskip("httpx2" if find_spec("httpx2") else "httpx")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from examples.showcase.server.app import create_app  # noqa: E402

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="item" type="xs:integer"/></xs:schema>"""
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>one</text></svg>'


@pytest.fixture
def registry(tmp_path):
    schema = tmp_path / "tiny.xsd"
    schema.write_text(XSD, encoding="utf-8")
    sample = tmp_path / "one.xml"
    sample.write_text("<item>3</item>", encoding="utf-8")
    return {
        "fake": {
            "schema": schema,
            "facts": lambda document: {"value": document.to_dict(typed=False)["$"]},
            "samples": {
                "one": {
                    "path": sample,
                    "renderer": lambda document: SVG,
                    "media_type": "image/svg+xml",
                    "note": "Tiny sample",
                },
            },
        },
        "missing": {
            "schema": tmp_path / "missing.xsd",
            "samples": {
                "one": {
                    "path": tmp_path / "missing.xml",
                    "renderer": lambda document: SVG,
                    "media_type": "image/svg+xml",
                },
            },
        },
    }


def test_landing_and_inventory(registry):
    with TestClient(create_app(registry=registry)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "one" in response.text and "/render/fake/one" in response.text
        assert "/validate/fake" in response.text
        response = client.get("/api/samples")
        assert response.status_code == 200
        assert response.json()[0] == {
            "domain": "fake",
            "sample": "one",
            "endpoint": "/render/fake/one",
            "media_type": "image/svg+xml",
            "note": "Tiny sample",
        }


def test_render_and_missing_routes(registry):
    application = create_app(registry=registry)
    assert application.state.schemas == {}
    with TestClient(application) as client:
        response = client.get("/render/fake/one")
        assert response.status_code == 200
        assert response.text == SVG
        assert response.headers["content-type"].startswith("image/svg+xml")
        schema = application.state.schemas[("fake", None)]
        assert client.get("/render/fake/one").text == SVG
        assert application.state.schemas[("fake", None)] is schema
        assert client.get("/render/fake/missing").status_code == 404
        assert client.get("/render/unknown/one").status_code == 404
        response = client.get("/render/missing/one")
        assert response.status_code == 503
        assert "download_sources" in response.text


def test_landing_escapes_registry_text(registry):
    registry["fake"]["samples"]["one"]["note"] = '<script>alert("x")</script>'
    with TestClient(create_app(registry=registry)) as client:
        text = client.get("/").text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_apps_do_not_share_schemas(registry):
    first, second = create_app(registry=registry), create_app(registry=registry)
    with TestClient(first) as client:
        client.get("/render/fake/one")
    with TestClient(second) as client:
        client.get("/render/fake/one")
    assert first.state.schemas[("fake", None)] is not second.state.schemas[("fake", None)]


def test_concurrent_requests_hold_schema_lock_through_facts_and_render(registry):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    application = create_app(registry=registry)
    barrier = Barrier(2, timeout=10)

    def render(document):
        assert application.state.schema_lock.locked()
        return SVG

    def facts(document):
        assert application.state.schema_lock.locked()
        return {"value": document.to_dict(typed=False)["$"]}

    registry["fake"]["samples"]["one"]["renderer"] = render
    registry["fake"]["facts"] = facts
    with TestClient(application) as client:

        def request(rendering):
            try:
                barrier.wait()
                if rendering:
                    return client.get("/render/fake/one").text
                return client.post("/validate/fake", content=b"<item>3</item>").json()
            except BaseException:
                barrier.abort()
                raise

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(request, [True, False]))
    assert results == [SVG, {"valid": True, "issues": [], "facts": {"value": "3"}}]


def test_default_facts_adapters_on_empty_bound_tree(registry):
    from examples.showcase.server.app import _default_registry

    from pyxsd import Schema

    document = Schema.compile(registry["fake"]["schema"]).parse(
        registry["fake"]["samples"]["one"]["path"]
    )
    defaults = _default_registry()
    assert defaults["citygml"]["facts"](document) == {"buildings": 0}
    assert defaults["opendrive"]["facts"](document) == {"roads": 0}
    assert defaults["bpmn"]["facts"](document) == {"shapes": 0, "edges": 0}
    assert defaults["ubl"]["facts"](document) == {"payable": None}
    assert defaults["score"]["facts"](document) == {"notes": 0}


def test_default_registry_is_curated_and_missing_cache_is_lazy(monkeypatch, tmp_path):
    from examples.showcase.server import app as module

    monkeypatch.setattr(module, "CACHE_ROOT", tmp_path)
    application = create_app()
    assert application.state.schemas == {}
    with TestClient(application) as client:
        items = client.get("/api/samples").json()
        assert {item["domain"] for item in items} == {
            "citygml",
            "opendrive",
            "bpmn",
            "ubl",
            "score",
        }
        assert {item["sample"] for item in items if item["domain"] == "bpmn"} == {
            "starter",
            "simple",
            "sequence-flows",
            "nested-subprocesses",
        }
        assert len(items) >= 12
        response = client.get("/render/ubl/01.01a-INVOICE_ubl")
        assert response.status_code == 503 and "download_sources" in response.text


@pytest.mark.parametrize(
    "media_type,extension", [("image/png", "png"), ("application/pdf", "pdf"), ("audio/wav", "wav")]
)
def test_file_response_and_temporary_cleanup(registry, media_type, extension):
    destinations = []

    def write(document, destination):
        destinations.append(destination)
        destination.write_bytes(b"artifact")
        return destination

    registry["fake"]["samples"]["one"]["formats"] = {
        extension: {"renderer": write, "media_type": media_type, "suffix": "." + extension}
    }
    with TestClient(create_app(registry=registry)) as client:
        response = client.get("/render/fake/one", params={"format": extension})
        assert response.status_code == 200
        assert response.headers["content-type"] == media_type
        assert response.content == b"artifact"
        assert not destinations[0].exists()
        assert not destinations[0].parent.exists()
        assert client.get("/render/fake/one?format=unknown").status_code == 400


def test_file_renderer_failure_cleans_up(registry):
    destinations = []

    def fail(document, destination):
        destinations.append(destination)
        raise ValueError("bad geometry")

    registry["fake"]["samples"]["one"].update(renderer=fail, suffix=".png", media_type="image/png")
    with TestClient(create_app(registry=registry)) as client:
        response = client.get("/render/fake/one")
    assert response.status_code == 422
    assert not destinations[0].parent.exists()


@pytest.mark.parametrize("minor,variant", [(4, "1.4"), (5, "1.5"), (6, "1.7"), (7, "1.7")])
def test_opendrive_version_sniff(minor, variant):
    from examples.showcase.server.app import _variant

    xml = f'<OpenDRIVE><header revMajor="1" revMinor="{minor}"/></OpenDRIVE>'.encode()
    assert _variant("opendrive", xml) == variant
    assert _variant("bpmn", b"ignored") is None


@pytest.mark.parametrize(
    "xml",
    [
        b"garbage",
        b"<OpenDRIVE/>",
        b'<OpenDRIVE><header revMajor="2" revMinor="4"/></OpenDRIVE>',
        b'<OpenDRIVE><header revMajor="1" revMinor="9"/></OpenDRIVE>',
        b'<!DOCTYPE OpenDRIVE [<!ENTITY x "bad">]><OpenDRIVE>&x;</OpenDRIVE>',
    ],
)
def test_opendrive_bad_version_input(xml):
    from xml.etree.ElementTree import ParseError

    from defusedxml.common import DefusedXmlException
    from examples.showcase.server.app import _variant

    with pytest.raises((ValueError, ParseError, DefusedXmlException)):
        _variant("opendrive", xml)


@pytest.mark.parametrize(
    "body", [b"garbage", b"<item>", b"\xff", b"", b"<item>no</item>", b"<wrong/>"]
)
def test_validate_bad_input_is_diagnostics_not_traceback(registry, body):
    with TestClient(create_app(registry=registry)) as client:
        response = client.post("/validate/fake", content=body)
        # A failed parse must not poison the cached schema for the next request.
        assert client.post("/validate/fake", content=b"<item>3</item>").json()["valid"]
    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is False
    assert result["issues"]
    for issue in result["issues"]:
        assert set(issue) == {"severity", "code", "message", "element", "phase"}
        assert issue["severity"] == "error" and issue["code"] and issue["message"]
        assert issue["phase"] in {"schema", "instance"}
    assert "facts" not in result


def test_validate_valid_and_limits(registry):
    application = create_app(registry=registry)
    with TestClient(application) as client:
        response = client.post("/validate/fake", content=b"<item>3</item>")
        assert response.status_code == 200
        assert response.json() == {"valid": True, "issues": [], "facts": {"value": "3"}}
        assert client.post("/validate/unknown", content=b"<item/>").status_code == 404
        response = client.post("/validate/missing", content=b"<item/>")
        assert response.status_code == 503 and "download_sources" in response.text
        response = client.post("/validate/fake", content=b" " * (8 * 1024 * 1024 + 1))
        assert response.status_code == 413


def test_validate_stream_limit_without_content_length(registry, monkeypatch):
    from examples.showcase.server import app as module

    monkeypatch.setattr(module, "MAX_XML_BYTES", 16)
    with TestClient(create_app(registry=registry)) as client:
        response = client.post("/validate/fake", content=iter([b" " * 8, b" " * 9]))
        assert response.status_code == 413
        assert client.post("/validate/fake", content=b"<item>3</item>  ").json()["valid"]


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE item [<!ENTITY x "3">]><item>&x;</item>',
        b'<!DOCTYPE item SYSTEM "file:///etc/passwd"><item>3</item>',
        b"<item>" * 130 + b"</item>" * 130,
    ],
)
def test_validate_parser_protections(registry, body):
    with TestClient(create_app(registry=registry)) as client:
        response = client.post("/validate/fake", content=body)
    assert response.status_code == 200
    assert not response.json()["valid"]
    assert response.json()["issues"]


def test_render_bad_xml_size_and_missing_extra(registry):
    sample = registry["fake"]["samples"]["one"]
    with TestClient(create_app(registry=registry)) as client:
        sample["path"].write_bytes(b"garbage")
        assert client.get("/render/fake/one").status_code == 422
        sample["path"].write_bytes(b" " * (8 * 1024 * 1024 + 1))
        assert client.get("/render/fake/one").status_code == 413
        sample["path"].write_bytes(b"<item>3</item>")

        def missing_extra(document):
            raise ImportError("optional package missing")

        sample["renderer"] = missing_extra
        response = client.get("/render/fake/one")
        assert response.status_code == 503 and "examples-reportlab" in response.text


def test_validate_opendrive_selects_schema_cache_key(registry):
    registry["opendrive"] = registry.pop("fake")
    registry["opendrive"]["facts"] = lambda document: {"roads": 0}
    registry["opendrive"]["schema"].write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="OpenDRIVE" type="xs:anyType"/></xs:schema>',
        encoding="utf-8",
    )
    application = create_app(registry=registry)
    with TestClient(application) as client:
        for minor, variant in [(4, "1.4"), (5, "1.5"), (7, "1.7")]:
            body = f'<OpenDRIVE><header revMajor="1" revMinor="{minor}"/></OpenDRIVE>'
            assert client.post("/validate/opendrive", content=body).json()["valid"]
            assert ("opendrive", variant) in application.state.schemas
        response = client.post("/validate/opendrive", content=b"<OpenDRIVE/>")
        assert response.status_code == 200 and not response.json()["valid"]


def test_exception_diagnostics_retain_validation_report(registry):
    from pyxsd import ValidationError
    from pyxsd.validation import ValidationReport

    report = ValidationReport()
    report.add_error("Domain constraint failed", code="domain-constraint", element="item")

    def fail(document):
        raise ValidationError("Invalid document", report)

    registry["fake"]["facts"] = fail
    with TestClient(create_app(registry=registry)) as client:
        response = client.post("/validate/fake", content=b"<item>3</item>")
    assert response.status_code == 200
    assert response.json() == {
        "valid": False,
        "issues": [
            {
                "severity": "error",
                "code": "domain-constraint",
                "message": "Domain constraint failed",
                "element": "item",
                "phase": "instance",
            }
        ],
    }


ROOT = Path(__file__).resolve().parents[1] / "examples/showcase"
CACHE = ROOT / ".cache"
SETUP = "uv run python examples/showcase/download_sources.py"


@pytest.mark.skipif(
    not (CACHE / "bpmn-schemas/BPMN20.xsd").is_file()
    or not (CACHE / "bpmn-data/starter.bpmn").is_file(),
    reason=SETUP,
)
def test_real_bpmn_render_and_validate():
    with TestClient(create_app()) as client:
        response = client.get("/render/bpmn/starter")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in response.text
        response = client.post(
            "/validate/bpmn", content=(CACHE / "bpmn-data/starter.bpmn").read_bytes()
        )
        assert response.json()["valid"]
        assert response.json()["facts"]["shapes"] > 0
        assert response.json()["facts"]["edges"] > 0


INVOICES = sorted((CACHE / "ubl-data").rglob("01.01a-INVOICE_ubl.xml"))


@pytest.mark.skipif(
    not INVOICES or not (CACHE / "ubl-schemas/maindoc/UBL-Invoice-2.3.xsd").is_file(), reason=SETUP
)
def test_real_invoice_render_and_validate():
    with TestClient(create_app()) as client:
        response = client.get("/render/ubl/01.01a-INVOICE_ubl")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<!DOCTYPE html>" in response.text
        response = client.post("/validate/ubl", content=INVOICES[0].read_bytes())
        assert response.status_code == 200 and response.json()["valid"]
        assert response.json()["facts"]["payable"] == "336.9"


@pytest.mark.skipif(not (CACHE / "musicxml-schemas/schema/musicxml.xsd").is_file(), reason=SETUP)
def test_real_score_render_and_validate():
    with TestClient(create_app()) as client:
        response = client.get("/render/score/chorale")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content[:4] == b"RIFF" and response.content[8:12] == b"WAVE"
        assert client.get("/render/score/chorale?format=svg").text.startswith("<svg")
        raw = (ROOT.parent / "musicxml/instance.xml").read_bytes()
        # Uploads use the strict parser profile: unlike the trusted render
        # source, the stock score's legacy external DOCTYPE is not accepted.
        assert not client.post("/validate/score", content=raw).json()["valid"]
        start, end = raw.index(b"<!DOCTYPE"), raw.index(b">", raw.index(b"<!DOCTYPE"))
        response = client.post("/validate/score", content=raw[:start] + raw[end + 1 :])
        assert response.json()["valid"]
        assert response.json()["facts"] == {"notes": 163}


@pytest.mark.parametrize(
    "domain,sample,schema_path,sample_path",
    [
        (
            "citygml",
            "lod2",
            "citygml-schemas/citygml/2.0/cityGMLBase.xsd",
            "citygml-schemas/citygml/examples/2.0/building/Building_LOD2-EPSG25832.gml",
        ),
        (
            "opendrive",
            "soderleden",
            "opendrive-schemas/opendrive_17_core.xsd",
            "opendrive-data/soderleden.xodr",
        ),
    ],
)
def test_real_geospatial_render_and_facts(domain, sample, schema_path, sample_path):
    if not (CACHE / schema_path).is_file() or not (CACHE / sample_path).is_file():
        pytest.skip(SETUP)
    application = create_app()
    with TestClient(application) as client:
        response = client.get(f"/render/{domain}/{sample}")
        assert response.status_code == 200 and "<svg" in response.text
        result = client.post(
            f"/validate/{domain}", content=(CACHE / sample_path).read_bytes()
        ).json()
        if domain == "citygml":
            # Published GML has documented schema/instance findings; rendering
            # those curated trees remains useful, validation must not hide them.
            assert not result["valid"] and result["issues"]
        else:
            assert (domain, "1.7") in application.state.schemas
            assert result["valid"] and result["facts"] == {"roads": 5}


@pytest.mark.parametrize(
    "domain,sample,format,dependency,magic",
    [
        ("citygml", "lod2", "png", "matplotlib", b"\x89PNG"),
        ("opendrive", "curves_elevation", "png", "matplotlib", b"\x89PNG"),
        ("ubl", "01.01a-INVOICE_ubl", "pdf", "reportlab", b"%PDF"),
    ],
)
def test_real_optional_file_render(domain, sample, format, dependency, magic):
    pytest.importorskip(dependency)
    from examples.showcase.common.schemas import DOMAINS
    from examples.showcase.common.sources import cache_path

    application = create_app()
    if not cache_path(DOMAINS[domain].schema_section).is_file():
        pytest.skip(SETUP)
    if not application.state.registry[domain]["samples"][sample]["path"].is_file():
        pytest.skip(SETUP)
    with TestClient(application) as client:
        response = client.get(f"/render/{domain}/{sample}?format={format}")
    assert response.status_code == 200
    assert response.content.startswith(magic)
