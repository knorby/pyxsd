"""HTTP contracts for the XRechnung family server, with cache-gated invoices."""

from importlib.util import find_spec

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2" if find_spec("httpx2") else "httpx")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from examples.xrechnung.common.sources import CACHE_ROOT, SETUP_HINT  # noqa: E402
from examples.xrechnung.server import create_app  # noqa: E402

from pyxsd import Schema  # noqa: E402

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="item" type="xs:integer"/></xs:schema>"""
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>one</text></svg>'
INVOICES = sorted((CACHE_ROOT / "ubl-data").rglob("01.01a-INVOICE_ubl.xml"))


@pytest.fixture
def isolated(tmp_path):
    schema_file = tmp_path / "tiny.xsd"
    schema_file.write_text(XSD, encoding="utf-8")
    sample = tmp_path / "one.xml"
    sample.write_text("<item>3</item>", encoding="utf-8")

    def factory(variant):
        assert variant is None
        return Schema.compile(schema_file)

    samples = {
        "one": {
            "path": sample,
            "renderer": lambda document: "<!DOCTYPE html><html><body>one</body></html>",
            "media_type": "text/html",
            "note": "Tiny invoice",
        }
    }
    return create_app(samples=samples, schema_factory=factory), factory


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
            "media_type": "text/html",
            "note": "Tiny invoice",
        }
    ]


def test_render_and_missing_routes(isolated):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.get("/render/one")
        assert response.status_code == 200
        assert response.text.startswith("<!DOCTYPE html>")
        assert client.get("/render/missing").status_code == 404
        assert client.get("/render/one?format=unknown").status_code == 400


def test_optional_pdf_response_and_temporary_cleanup(isolated):
    application, _ = isolated
    destinations = []

    def write(document, destination):
        destinations.append(destination)
        destination.write_bytes(b"%PDF-1.4\nartifact")
        return destination

    application.state.samples["one"]["formats"] = {
        "pdf": {"renderer": write, "media_type": "application/pdf", "suffix": ".pdf"}
    }
    with TestClient(application) as client:
        response = client.get("/render/one", params={"format": "pdf"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")
        assert not destinations[0].exists()
        assert not destinations[0].parent.exists()


def test_file_renderer_failure_cleans_up(isolated):
    application, _ = isolated
    destinations = []

    def fail(document, destination):
        destinations.append(destination)
        raise ValueError("bad invoice")

    application.state.samples["one"]["formats"] = {
        "pdf": {"renderer": fail, "media_type": "application/pdf", "suffix": ".pdf"}
    }
    with TestClient(application) as client:
        response = client.get("/render/one?format=pdf")
    assert response.status_code == 422
    assert not destinations[0].parent.exists()


def test_missing_reportlab_is_503(isolated):
    application, _ = isolated

    def missing(document, destination):
        raise ImportError("reportlab is not installed")

    application.state.samples["one"]["formats"] = {
        "pdf": {"renderer": missing, "media_type": "application/pdf", "suffix": ".pdf"}
    }
    with TestClient(application) as client:
        response = client.get("/render/one?format=pdf")
    assert response.status_code == 503
    assert "examples-reportlab" in response.text


def test_validate_valid_and_invalid(isolated):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=b"<item>3</item>")
        assert response.json() == {
            "valid": True,
            "issues": [],
            "facts": {"payable": None},
        }
        response = client.post("/validate", content=b"<item>no</item>")
        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert response.json()["issues"]
        assert "facts" not in response.json()


@pytest.mark.parametrize(
    "body",
    [
        b"garbage",
        b"<item>",
        b"\xff",
        b"",
        b"<wrong/>",
        b'<?xml version="1.0" encoding="bogus"?><item>3</item>',
    ],
)
def test_bad_input_is_diagnostics_not_traceback(isolated, body):
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=body)
        assert client.post("/validate", content=b"<item>3</item>").json()["valid"]
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
        b'<!DOCTYPE item [<!ENTITY x "3">]><item>&x;</item>',
        b'<!DOCTYPE item SYSTEM "file:///etc/passwd"><item>3</item>',
        b"<item>" * 130 + b"</item>" * 130,
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
    from examples.xrechnung import server

    monkeypatch.setattr(server, "MAX_XML_BYTES", 16)
    application, _ = isolated
    with TestClient(application) as client:
        response = client.post("/validate", content=iter([b" " * 8, b" " * 9]))
        assert response.status_code == 413
        assert client.post("/validate", content=b"<item>3</item>  ").json()["valid"]


def test_oversized_body_and_curated_sample(isolated):
    application, _ = isolated
    sample = application.state.samples["one"]
    with TestClient(application) as client:
        assert client.post("/validate", content=b" " * (8 * 1024 * 1024 + 1)).status_code == 413
        sample["path"].write_bytes(b" " * (8 * 1024 * 1024 + 1))
        assert client.get("/render/one").status_code == 413
        sample["path"].write_bytes(b"garbage")
        assert client.get("/render/one").status_code == 422
        sample["path"].write_text("<item>3</item>", encoding="utf-8")


def test_apps_do_not_share_schemas(isolated):
    _, factory = isolated
    first, second = create_app(schema_factory=factory), create_app(schema_factory=factory)
    with TestClient(first) as client:
        client.post("/validate", content=b"<item>3</item>")
    with TestClient(second) as client:
        client.post("/validate", content=b"<item>3</item>")
    assert first.state.schemas[None] is not second.state.schemas[None]


def test_concurrent_requests_hold_schema_lock(isolated):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    application, _ = isolated
    barrier = Barrier(2, timeout=10)

    def render(document):
        assert application.state.schema_lock.locked()
        return "<!DOCTYPE html><html><body>one</body></html>"

    def facts(document):
        assert application.state.schema_lock.locked()
        return {"payable": "1.00"}

    application.state.samples["one"]["renderer"] = render
    application.state.facts = facts
    with TestClient(application) as client:

        def request(rendering):
            try:
                barrier.wait()
                if rendering:
                    return client.get("/render/one").text
                return client.post("/validate", content=b"<item>3</item>").json()
            except BaseException:
                barrier.abort()
                raise

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(request, [True, False]))
    assert results[0].startswith("<!DOCTYPE html>")
    assert results[1] == {"valid": True, "issues": [], "facts": {"payable": "1.00"}}


def test_missing_sources_have_sanitized_setup_response(tmp_path):
    secret = str(tmp_path / "private" / "missing.xsd")

    def missing_schema(variant):
        raise FileNotFoundError(secret)

    hidden = tmp_path / "missing.xml"
    samples = {
        "missing": {
            "path": hidden,
            "renderer": lambda document: "<html></html>",
            "media_type": "text/html",
        }
    }
    application = create_app(samples=samples, schema_factory=missing_schema)
    with TestClient(application) as client:
        render = client.get("/render/missing")
        validate = client.post("/validate", content=b"<item>3</item>")
    for response in (render, validate):
        assert response.status_code == 503
        assert "examples/xrechnung/download_sources.py" in response.text
        assert secret not in response.text


def test_facts_pyxsd_error_without_report_is_sanitized(isolated):
    from pyxsd import PyXSDError

    application, _ = isolated

    def fail(document):
        raise PyXSDError("domain check failed")

    application.state.facts = fail
    with TestClient(application) as client:
        response = client.post("/validate", content=b"<item>3</item>")
    assert response.status_code == 200
    assert response.json()["issues"][0]["message"] == "domain check failed"


def test_facts_report_validation_failure_without_traceback(isolated):
    from pyxsd import ValidationError
    from pyxsd.validation import ValidationReport

    application, _ = isolated
    report = ValidationReport()
    report.add_error("Domain constraint failed", code="domain-constraint", element="item")

    def fail(document):
        raise ValidationError("Invalid document", report)

    application.state.facts = fail
    with TestClient(application) as client:
        response = client.post("/validate", content=b"<item>3</item>")
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["issues"][0]["code"] == "domain-constraint"


@pytest.mark.skipif(
    not INVOICES or not (CACHE_ROOT / "ubl-schemas/maindoc/UBL-Invoice-2.3.xsd").is_file(),
    reason=SETUP_HINT,
)
def test_real_invoice_render_and_validate():
    with TestClient(create_app()) as client:
        response = client.get("/render/01.01a-INVOICE_ubl")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<!DOCTYPE html>" in response.text
        assert "336.90" in response.text
        assert "pyxsd" not in response.text.lower()
        assert "<!--" not in response.text
        result = client.post("/validate", content=INVOICES[0].read_bytes()).json()
    assert result["valid"]
    assert result["facts"] == {"payable": "336.9"}


@pytest.mark.skipif(
    not INVOICES or not (CACHE_ROOT / "ubl-schemas/maindoc/UBL-Invoice-2.3.xsd").is_file(),
    reason=SETUP_HINT,
)
def test_real_invoice_pdf():
    pytest.importorskip("reportlab")
    with TestClient(create_app()) as client:
        response = client.get("/render/01.01a-INVOICE_ubl?format=pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_render_failure_does_not_echo_exception_text(isolated):
    from pyxsd import PyXSDError

    application, _ = isolated

    def fail(document):
        raise PyXSDError("secret stack frame at /private/example")

    application.state.samples["one"]["renderer"] = fail
    with TestClient(application) as client:
        response = client.get("/render/one")
    assert response.status_code == 422
    assert "secret stack frame" not in response.text
    assert "/private/example" not in response.text
