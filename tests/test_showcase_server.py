"""Offline HTTP contracts, with optional pinned-corpus smoke tests."""

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
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
            "facts": lambda document: {"value": document.to_dict(typed=False)},
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
