"""Loopback HTTP and RSA issuer fixtures; no external network or credentials."""

import http.client
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
PUBLIC_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
PUBLIC_JWK["kid"] = "test-key"
JWKS = {"keys": [PUBLIC_JWK]}
ISSUER = "https://issuer.example"
AUDIENCE = "https://mcp.example"


def token(
    *,
    subject="alice",
    issuer=ISSUER,
    audience=AUDIENCE,
    scopes=("pyxsd:read", "pyxsd:upload"),
    expires=None,
    algorithm="RS256",
    key=PRIVATE_PEM,
    kid="test-key",
):
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + 300 if expires is None else expires,
        "scope": " ".join(scopes),
    }
    if subject is not None:
        payload["sub"] = subject
    return jwt.encode(payload, key, algorithm=algorithm, headers={"kid": kid})


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


def request(port, method, path, *, body=b"", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        content = response.read()
        return Response(response.status, dict(response.getheaders()), content)
    finally:
        connection.close()


@contextmanager
def serve(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("HTTP test server did not start")
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


class MCPClient:
    def __init__(self, port, token_value, protocol="2025-06-18"):
        self.port, self.token, self.protocol = port, token_value, protocol
        self.session = None
        self.next_id = 0

    @property
    def headers(self):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session is not None:
            headers["Mcp-Session-Id"] = self.session
        return headers

    def initialize(self):
        self.next_id += 1
        response = request(
            self.port,
            "POST",
            "/mcp",
            body=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.next_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self.protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "pyxsd-test", "version": "1"},
                    },
                }
            ).encode(),
            headers=self.headers,
        )
        assert response.status == 200, response.body
        self.session = response.headers.get("Mcp-Session-Id") or response.headers.get(
            "mcp-session-id"
        )
        initialized = request(
            self.port,
            "POST",
            "/mcp",
            body=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
            headers=self.headers,
        )
        assert initialized.status in (200, 202, 204), initialized.body
        return response.json()

    def call(self, method, params=None):
        self.next_id += 1
        response = request(
            self.port,
            "POST",
            "/mcp",
            body=json.dumps(
                {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
            ).encode(),
            headers=self.headers,
        )
        assert response.status == 200, response.body
        return response.json()

    def tool(self, name, arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments})

    def create_workspace(self):
        result = self.tool(
            "pyxsd_manage_workspace", {"action": "create", "idempotency_key": "http"}
        )
        assert result["result"]["isError"] is False, result
        return result["result"]["structuredContent"]["data"]["id"]
