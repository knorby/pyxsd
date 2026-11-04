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
