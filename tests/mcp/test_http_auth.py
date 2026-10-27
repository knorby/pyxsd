"""Real HTTP authentication, issuer validation and transport security tests."""

import json
import time

import jwt
import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("jwt")
pytest.importorskip("uvicorn")

from tests.mcp.http_support import (
    AUDIENCE,
    ISSUER,
    JWKS,
    PRIVATE_PEM,
    MCPClient,
    request,
    serve,
    token,
)

from pyxsd.mcp.auth import ConfiguredTokenVerifier
from pyxsd.mcp.config import HTTPSettings, Settings
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.http import create_http_app
from pyxsd.mcp.server import Services


def settings(tmp_path, **kwargs):
    arguments = {
        "issuer_url": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": "https://issuer.example/jwks.json",
        "allowed_hosts": {"127.0.0.1:*"},
        "allowed_origins": {"https://client.example"},
        **kwargs,
    }
    return Settings(
        state_dir=tmp_path / "state", transport="streamable-http", http=HTTPSettings(**arguments)
    )


def verifier(**kwargs):
    arguments = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": "https://issuer.example/jwks.json",
        "fetcher": lambda _url: json.dumps(JWKS).encode(),
        **kwargs,
    }
    return ConfiguredTokenVerifier(**arguments)


def test_verifier_accepts_and_rejects_real_signed_tokens():
    async def scenario():
        accepted = await verifier().verify_token(token())
        assert accepted.subject == "alice"
        assert accepted.client_id == "alice"
        assert accepted.scopes == ["pyxsd:read", "pyxsd:upload"]
        listed = await verifier().verify_token(token(audience=[AUDIENCE]))
        assert listed.resource == AUDIENCE
        assert await verifier().verify_token("not-a-token") is None
        assert await verifier().verify_token("x" * 8193) is None
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 300,
            "sub": "alice",
            "scope": ["pyxsd:read"],
        }
        non_string = jwt.encode(
            payload, PRIVATE_PEM, algorithm="RS256", headers={"kid": "test-key"}
        )
        assert await verifier().verify_token(non_string) is None
        payload["sub"] = 123
        numeric_subject = jwt.encode(
            payload, PRIVATE_PEM, algorithm="RS256", headers={"kid": "test-key"}
        )
        assert await verifier().verify_token(numeric_subject) is None
        for value in (
            token(issuer="https://wrong.example"),
            token(audience="https://wrong.example"),
            token(algorithm="HS256", key="x" * 32),
            token(expires=int(time.time()) - 1),
            token(subject=None),
            token(kid="unknown"),
        ):
            assert await verifier().verify_token(value) is None

    import anyio

    anyio.run(scenario)


def test_bounded_jwks_refresh_failure_and_static_configuration():
    calls = []

    def unavailable(_url):
        calls.append(1)
        raise OSError("network disabled")

    failing = ConfiguredTokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://issuer.example/jwks.json",
        fetcher=unavailable,
    )
    with pytest.raises(ToolError, match="auth_unavailable"):
        import anyio

        anyio.run(failing.preflight)
    assert len(calls) == 1
    for options in (
        {"issuer": "http://issuer.example"},
        {"jwks_url": "http://issuer.example/jwks.json"},
        {"algorithms": ("HS256",)},
        {"audience": ""},
        {"refresh_seconds": 0},
        {"max_jwks_bytes": 0},
    ):
        with pytest.raises(ToolError, match="invalid_argument"):
            verifier(**options)


def test_jwks_fetch_redirect_limits_and_empty_documents(monkeypatch):
    from pyxsd.mcp import auth as auth_module

    class Response:
        def __init__(self, url, content):
            self.url, self.content = url, content

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return self.url

        def read(self, _limit):
            return self.content

    monkeypatch.setattr(
        auth_module.urllib.request,
        "urlopen",
        lambda url, timeout: Response(url, b"{}"),
    )
    assert verifier()._fetch("https://issuer.example/jwks.json") == b"{}"
    monkeypatch.setattr(
        auth_module.urllib.request,
        "urlopen",
        lambda url, timeout: Response("https://redirect.example/jwks.json", b"{}"),
    )
    with pytest.raises(ToolError, match="auth_unavailable"):
        verifier()._fetch("https://issuer.example/jwks.json")
    monkeypatch.setattr(
        auth_module.urllib.request,
        "urlopen",
        lambda url, timeout: Response(url, b"x" * (2**20 + 1)),
    )
    with pytest.raises(ToolError, match="auth_unavailable"):
        verifier()._fetch("https://issuer.example/jwks.json")
    monkeypatch.setattr(
        auth_module.urllib.request,
        "urlopen",
        lambda url, timeout: (_ for _ in ()).throw(OSError("down")),
    )
    with pytest.raises(ToolError, match="auth_unavailable"):
        verifier()._fetch("https://issuer.example/jwks.json")
    for document in (
        {"keys": []},
        {"keys": [{key: value for key, value in JWKS["keys"][0].items() if key != "kid"}]},
    ):
        failing = ConfiguredTokenVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_url="https://issuer.example/jwks.json",
            fetcher=lambda _url, document=document: json.dumps(document).encode(),
        )
        with pytest.raises(ToolError, match="auth_unavailable"):
            import anyio

            anyio.run(failing.preflight)
    from types import SimpleNamespace

    monkeypatch.setattr(
        auth_module.jwt.PyJWKSet,
        "from_dict",
        lambda _document: SimpleNamespace(keys=[]),
    )
    with pytest.raises(ToolError, match="auth_unavailable"):
        import anyio

        anyio.run(verifier().preflight)

    def denied(_url):
        raise ToolError("auth_unavailable", "denied")

    with pytest.raises(ToolError, match="auth_unavailable"):
        import anyio

        anyio.run(
            ConfiguredTokenVerifier(
                issuer=ISSUER,
                audience=AUDIENCE,
                jwks_url="https://issuer.example/jwks.json",
                fetcher=denied,
            ).preflight
        )
    monkeypatch.setattr(
        auth_module.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": 123,
            "exp": time.time() + 300,
            "aud": AUDIENCE,
            "scope": "",
        },
    )
    import anyio

    async def key(_self, _kid):
        return object()

    monkeypatch.setattr(ConfiguredTokenVerifier, "_get_key", key)
    assert anyio.run(verifier().verify_token, token()) is None


def test_startup_requires_auth_or_explicit_loopback(tmp_path):
    with pytest.raises(ToolError, match="auth_required"):
        missing = settings(tmp_path / "missing")
        create_http_app(missing, Services(missing), None)
    development = settings(tmp_path, loopback_dev=True)
    app = create_http_app(development, Services(development), None)
    assert app is not None


def test_loopback_dev_workflow_and_configuration_rejections(tmp_path):
    development = settings(tmp_path, loopback_dev=True)
    app = create_http_app(development, Services(development), None)
    with serve(app) as port:
        client = MCPClient(port, token())
        client.initialize()
        assert client.create_workspace()
    for options in ({"allowed_hosts": {"evil.example"}},):
        with pytest.raises(ToolError, match="invalid_argument"):
            invalid = settings(tmp_path / "invalid", loopback_dev=True, **options)
            create_http_app(invalid, Services(invalid), None)
    with pytest.raises(ToolError, match="auth_required"):
        no_http = Settings(state_dir=tmp_path / "no-http", transport="streamable-http")
        create_http_app(no_http, Services(no_http), None)
    with pytest.raises(ToolError, match="execution_unavailable"):
        local = settings(tmp_path / "local")
        services = Services(local)
        local = local.model_copy(update={"execution_backend": "trusted-local"})
        create_http_app(local, services, verifier())
    with pytest.raises(ToolError, match="forbidden"):
        unauthenticated = settings(tmp_path / "identity")
        services = Services(unauthenticated)
        services.current_principal()
    import anyio

    anyio.run(services.aclose)


def test_http_metadata_challenges_scopes_and_protocol_versions(tmp_path):
    config = settings(tmp_path)
    app = create_http_app(config, Services(config), verifier())
    with serve(app) as port:
        metadata = request(port, "GET", "/.well-known/oauth-protected-resource")
        assert metadata.status == 200
        assert ISSUER in metadata.body.decode()
        unauthenticated = request(
            port, "POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"}
        )
        assert unauthenticated.status == 401
        assert "resource_metadata" in unauthenticated.headers["www-authenticate"]
        for protocol in ("2025-03-26", "2025-06-18"):
            client = MCPClient(port, token(), protocol)
            assert client.initialize()["result"]["serverInfo"]["name"] == "pyxsd"
            workspace = client.create_workspace()
            assert workspace
        insufficient = MCPClient(port, token(scopes=("pyxsd:read",)))
        insufficient.initialize()
        denied = insufficient.tool("pyxsd_manage_workspace", {"action": "create"})
        assert denied["result"]["isError"] is True
        assert denied["result"]["structuredContent"]["data"]["fault"]["code"] == "forbidden"


def test_http_host_origin_forwarded_and_auth_rejections(tmp_path):
    config = settings(tmp_path)
    app = create_http_app(config, Services(config), verifier())
    with serve(app) as port:
        for bad_token in (
            token(issuer="https://wrong.example"),
            token(audience="https://wrong.example"),
            token(algorithm="HS256", key="x" * 32),
            token(expires=int(time.time()) - 1),
            token(subject=None),
            token(kid="unknown"),
        ):
            response = request(
                port,
                "POST",
                "/mcp",
                body=b"{}",
                headers={
                    "Authorization": f"Bearer {bad_token}",
                    "Content-Type": "application/json",
                },
            )
            assert response.status == 401
        valid = MCPClient(port, token())
        valid.initialize()
        host = request(
            port, "GET", "/.well-known/oauth-protected-resource", headers={"Host": "evil.example"}
        )
        assert host.status in (400, 421)
        origin = request(
            port,
            "POST",
            "/mcp",
            body=b"{}",
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        assert origin.status in (400, 403)
        forwarded = request(
            port,
            "GET",
            "/.well-known/oauth-protected-resource",
            headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"},
        )
        assert forwarded.status == 200
