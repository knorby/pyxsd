"""HTTP contracts for the OpenDRIVE family server, with cache-gated roads."""

from importlib.util import find_spec

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2" if find_spec("httpx2") else "httpx")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from examples.opendrive.common.sources import CACHE_ROOT, SETUP_HINT  # noqa: E402
from examples.opendrive.server import create_app  # noqa: E402

from pyxsd import Schema  # noqa: E402

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="OpenDRIVE"><xs:complexType><xs:sequence>
<xs:element name="header"><xs:complexType><xs:attribute name="revMajor" type="xs:string"/>
<xs:attribute name="revMinor" type="xs:string"/></xs:complexType></xs:element>
</xs:sequence></xs:complexType></xs:element></xs:schema>"""
SAMPLE = b'<OpenDRIVE><header revMajor="1" revMinor="4"/></OpenDRIVE>'
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>one</text></svg>'


@pytest.fixture
def isolated(tmp_path):
    schema_file = tmp_path / "tiny.xsd"
    schema_file.write_text(XSD, encoding="utf-8")
    sample = tmp_path / "one.xodr"
    sample.write_bytes(SAMPLE)

    def factory(variant):
        assert variant == "1.4"
        return Schema.compile(schema_file)

    samples = {
        "one": {
            "path": sample,
            "renderer": lambda document: SVG,
            "media_type": "image/svg+xml",
            "note": "Tiny road",
        }
    }
    return create_app(samples=samples, schema_factory=factory), factory


@pytest.mark.parametrize("minor,variant", [("4", "1.4"), ("5", "1.5"), ("6", "1.7"), ("7", "1.7")])
def test_revision_selects_schema(minor, variant):
    from examples.opendrive.server import _variant

    xml = f'<OpenDRIVE><header revMajor="1" revMinor="{minor}"/></OpenDRIVE>'.encode()
    assert _variant(xml) == variant


@pytest.mark.parametrize(
    "xml",
    [
        b"garbage",
        b"<OpenDRIVE/>",
        b'<OpenDRIVE><header revMajor="2" revMinor="0"/></OpenDRIVE>',
        b'<OpenDRIVE><header revMajor="1" revMinor="9"/></OpenDRIVE>',
        b'<!DOCTYPE OpenDRIVE [<!ENTITY x "bad">]><OpenDRIVE>&x;</OpenDRIVE>',
    ],
)
def test_bad_revision_input(xml):
    from xml.etree.ElementTree import ParseError

    from defusedxml.common import DefusedXmlException
    from examples.opendrive.server import _variant

    with pytest.raises((ValueError, ParseError, DefusedXmlException)):
        _variant(xml)


def test_landing_and_inventory(isolated):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.get("/")
        inventory = client.get("/api/samples").json()
    assert response.status_code == 200
    assert "/render/one" in response.text
    assert "/validate" in response.text
    assert inventory == [
        {
            "sample": "one",
            "endpoint": "/render/one",
            "media_type": "image/svg+xml",
            "note": "Tiny road",
        }
    ]


def test_render_and_missing_routes(isolated):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.get("/render/one")
        assert response.status_code == 200
        assert response.text == SVG
        assert client.get("/render/missing").status_code == 404
        assert client.get("/render/one?format=unknown").status_code == 400


def test_optional_png_response_and_temporary_cleanup(isolated):
    application, _ = isolated
    destinations = []

    def write(document, destination):
        destinations.append(destination)
        destination.write_bytes(b"\x89PNG\r\n\x1a\nartifact")
        return destination

    application.state.samples["one"]["formats"] = {
        "png": {"renderer": write, "media_type": "image/png", "suffix": ".png"}
    }
    with TestClient(application) as client:
        response = client.get("/render/one", params={"format": "png"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")
        assert not destinations[0].exists()
        assert not destinations[0].parent.exists()


def test_file_renderer_failure_cleans_up(isolated):
    application, _ = isolated
    destinations = []

    def fail(document, destination):
        destinations.append(destination)
        raise ValueError("bad profiles")

    application.state.samples["one"]["formats"] = {
        "png": {"renderer": fail, "media_type": "image/png", "suffix": ".png"}
    }
    with TestClient(application) as client:
        response = client.get("/render/one?format=png")
    assert response.status_code == 422
    assert not destinations[0].parent.exists()


def test_missing_matplotlib_is_503(isolated):
    application, _ = isolated

    def missing(document, destination):
        raise ImportError("matplotlib is not installed")

    application.state.samples["one"]["formats"] = {
        "png": {"renderer": missing, "media_type": "image/png", "suffix": ".png"}
    }
    with TestClient(application) as client:
        response = client.get("/render/one?format=png")
    assert response.status_code == 503
    assert "examples-matplotlib" in response.text


def test_validate_valid_and_invalid(isolated):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=SAMPLE)
        assert response.json() == {
            "valid": True,
            "issues": [],
            "facts": {"roads": 0},
        }
        response = client.post(
            "/validate",
            content=b'<OpenDRIVE><header revMajor="1" revMinor="4"/><wrong/></OpenDRIVE>',
        )
        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert response.json()["issues"]
        assert "facts" not in response.json()


@pytest.mark.parametrize(
    "body",
    [
        b"garbage",
        b"<OpenDRIVE/>",
        b"\xff",
        b"",
        b"<wrong/>",
        b'<?xml version="1.0" encoding="bogus"?><OpenDRIVE/>',
    ],
)
def test_bad_input_is_diagnostics_not_traceback(isolated, body):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=body)
        assert client.post("/validate", content=SAMPLE).json()["valid"]
    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is False
    assert result["issues"]
    for issue in result["issues"]:
        assert set(issue) == {"severity", "code", "message", "element", "phase"}
        assert issue["severity"] == "error" and issue["code"] and issue["message"]
        assert issue["phase"] in {"schema", "instance"}


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE OpenDRIVE [<!ENTITY x "bad">]><OpenDRIVE>&x;</OpenDRIVE>',
        b'<!DOCTYPE OpenDRIVE SYSTEM "file:///etc/passwd"><OpenDRIVE/>',
        b"<OpenDRIVE>" * 130 + b"</OpenDRIVE>" * 130,
    ],
)
def test_parser_protections(isolated, body):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=body)
    assert response.status_code == 200
    assert not response.json()["valid"]
    assert response.json()["issues"]


def test_stream_limit_without_content_length(isolated, monkeypatch):
    from examples.opendrive import server

    monkeypatch.setattr(server, "MAX_XML_BYTES", 64)
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=iter([b" " * 32, b" " * 33]))
        assert response.status_code == 413
        assert client.post("/validate", content=SAMPLE + b" ").json()["valid"]


def test_oversized_body_and_curated_sample(isolated):
    application, _ = isolated
    sample = application.state.samples["one"]
    with TestClient(application) as client:
        assert client.post("/validate", content=b" " * (8 * 1024 * 1024 + 1)).status_code == 413
        sample["path"].write_bytes(b" " * (8 * 1024 * 1024 + 1))
        assert client.get("/render/one").status_code == 413
        sample["path"].write_bytes(b"garbage")
        assert client.get("/render/one").status_code == 422
        sample["path"].write_bytes(SAMPLE)


def test_validate_uses_variant_specific_schema_cache(tmp_path):
    schema_file = tmp_path / "tiny.xsd"
    schema_file.write_text(XSD, encoding="utf-8")
    sample = tmp_path / "one.xodr"
    sample.write_bytes(SAMPLE)
    variants = []

    def factory(variant):
        variants.append(variant)
        return Schema.compile(schema_file)

    samples = {
        "one": {
            "path": sample,
            "renderer": lambda document: SVG,
            "media_type": "image/svg+xml",
        }
    }
    application = create_app(samples=samples, schema_factory=factory)
    with TestClient(application) as client:
        for minor in ("4", "5", "7", "6"):
            body = f'<OpenDRIVE><header revMajor="1" revMinor="{minor}"/></OpenDRIVE>'
            assert client.post("/validate", content=body).json()["valid"]
        response = client.post(
            "/validate", content=b'<OpenDRIVE><header revMajor="1" revMinor="8"/></OpenDRIVE>'
        )
    assert variants == ["1.4", "1.5", "1.7"]
    assert response.status_code == 200 and not response.json()["valid"]
    assert set(application.state.schemas) == {"1.4", "1.5", "1.7"}


def test_apps_do_not_share_schemas(isolated):
    _, factory = isolated
    first, second = create_app(schema_factory=factory), create_app(schema_factory=factory)
    with TestClient(first) as client:
        client.post("/validate", content=SAMPLE)
    with TestClient(second) as client:
        client.post("/validate", content=SAMPLE)
    assert first.state.schemas["1.4"] is not second.state.schemas["1.4"]


def test_concurrent_requests_hold_schema_lock(isolated):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    application, _ = isolated
    barrier = Barrier(2, timeout=10)

    def render(document):
        assert application.state.schema_lock.locked()
        return SVG

    def facts(document):
        assert application.state.schema_lock.locked()
        return {"roads": 1}

    application.state.samples["one"]["renderer"] = render
    application.state.facts = facts
    with TestClient(application) as client:

        def request(rendering):
            try:
                barrier.wait()
                if rendering:
                    return client.get("/render/one").text
                return client.post("/validate", content=SAMPLE).json()
            except BaseException:
                barrier.abort()
                raise

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(request, [True, False]))
    assert results == [SVG, {"valid": True, "issues": [], "facts": {"roads": 1}}]


def test_missing_sources_have_sanitized_setup_response(tmp_path):
    secret = str(tmp_path / "private" / "missing.xsd")

    def missing_schema(variant):
        raise FileNotFoundError(secret)

    hidden = tmp_path / "missing.xodr"
    samples = {
        "missing": {
            "path": hidden,
            "renderer": lambda document: SVG,
            "media_type": "image/svg+xml",
        }
    }
    application = create_app(samples=samples, schema_factory=missing_schema)
    with TestClient(application) as client:
        render = client.get("/render/missing")
        validate = client.post("/validate", content=SAMPLE)
    for response in (render, validate):
        assert response.status_code == 503
        assert "examples/opendrive/download_sources.py" in response.text
        assert secret not in response.text


def test_facts_pyxsd_error_without_report_is_sanitized(isolated):
    from pyxsd import PyXSDError

    application, _ = isolated

    def fail(document):
        raise PyXSDError("domain check failed")

    application.state.facts = fail
    with TestClient(application) as client:
        response = client.post("/validate", content=SAMPLE)
    assert response.status_code == 200
    assert response.json()["issues"][0]["message"] == "domain check failed"


def test_facts_report_validation_failure_without_traceback(isolated):
    from pyxsd import ValidationError
    from pyxsd.validation import ValidationReport

    application, _ = isolated
    report = ValidationReport()
    report.add_error("Domain constraint failed", code="domain-constraint", element="OpenDRIVE")

    def fail(document):
        raise ValidationError("Invalid document", report)

    application.state.facts = fail
    with TestClient(application) as client:
        response = client.post("/validate", content=SAMPLE)
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["issues"][0]["code"] == "domain-constraint"


@pytest.mark.skipif(
    not (CACHE_ROOT / "opendrive-schemas/opendrive_17_core.xsd").is_file()
    or not (CACHE_ROOT / "opendrive-data/Town01.xodr").is_file()
    or not (CACHE_ROOT / "opendrive-data/soderleden.xodr").is_file(),
    reason=SETUP_HINT,
)
def test_real_opendrive_render_and_validate():
    application = create_app()
    with TestClient(application) as client:
        response = client.get("/render/Town01")
        assert response.status_code == 200
        assert "<svg" in response.text
        town = client.post(
            "/validate", content=(CACHE_ROOT / "opendrive-data/Town01.xodr").read_bytes()
        ).json()
        highway = client.post(
            "/validate", content=(CACHE_ROOT / "opendrive-data/soderleden.xodr").read_bytes()
        ).json()
    assert town["valid"] and town["facts"] == {"roads": 98}
    assert highway["valid"] and highway["facts"] == {"roads": 5}
    assert set(application.state.schemas) == {"1.4", "1.7"}


@pytest.mark.skipif(
    not (CACHE_ROOT / "opendrive-schemas/opendrive_17_core.xsd").is_file()
    or not (CACHE_ROOT / "opendrive-data/curves_elevation.xodr").is_file(),
    reason=SETUP_HINT,
)
def test_real_opendrive_png():
    pytest.importorskip("matplotlib")
    with TestClient(create_app()) as client:
        response = client.get("/render/curves_elevation?format=png")
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")
