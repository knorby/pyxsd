"""Bounded HTTPS transport behavior, exercised through fake connections.

Nothing in this module creates a socket: every test replaces
``http.client.HTTPSConnection`` with a fake and inspects the request the
transport built. Redirects are returned, never followed, so the caller's
policy remains the only place that authorizes a next hop.
"""

import http.client
import ssl

import pytest

from pyxsd.remote import (
    HttpClientTransport,
    RemoteLimits,
    RemoteResponse,
    RemoteTransportError,
)

URL = "https://schemas.example/a/main.xsd"


class FakeSocket:
    """Records the socket read timeout installed after connecting."""

    def __init__(self) -> None:
        self.timeouts = []

    def settimeout(self, value) -> None:
        self.timeouts.append(value)


class FakeResponse:
    """A minimal ``http.client.HTTPResponse`` with bounded reads."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers=(),
        body: bytes = b"",
        read_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._headers = list(headers)
        self._body = body
        self._offset = 0
        self._read_error = read_error
        self.read_sizes = []
        self.closed = False

    def getheaders(self):
        return list(self._headers)

    def read(self, amount: int) -> bytes:
        self.read_sizes.append(amount)
        if self._read_error is not None:
            raise self._read_error
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def install(monkeypatch, response: FakeResponse, *, connect_error=None, getresponse_error=None):
    """Replace ``HTTPSConnection`` and return the list of created fakes."""
    created = []

    class FakeConnection:
        def __init__(self, host, port=None, *, timeout=None, context=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.context = context
            self.response = response
            self.connect_error = connect_error
            self.getresponse_error = getresponse_error
            self.sock = None
            self.requests = []
            self.connect_calls = 0
            self.closed = False
            created.append(self)

        def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_error is not None:
                raise self.connect_error
            self.sock = FakeSocket()

        def request(self, method: str, target: str) -> None:
            self.requests.append((method, target))

        def getresponse(self) -> FakeResponse:
            if self.getresponse_error is not None:
                raise self.getresponse_error
            return self.response

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)
    return created


def test_fetch_returns_one_https_response_and_closes_resources(monkeypatch):
    response = FakeResponse(
        headers=[("Content-Type", "application/xml")],
        body=b"<xs:schema/>",
    )
    created = install(monkeypatch, response)
    limits = RemoteLimits()

    result = HttpClientTransport().fetch(URL, limits=limits)

    assert isinstance(result, RemoteResponse)
    assert result.url == URL
    assert result.status == 200
    assert result.headers["Content-Type"] == "application/xml"
    assert result.content == b"<xs:schema/>"
    connection = created[0]
    assert len(created) == 1
    assert connection.host == "schemas.example"
    assert connection.port == 443
    assert connection.requests == [("GET", "/a/main.xsd")]
    assert response.read_sizes == [limits.response_bytes + 1]
    assert response.closed
    assert connection.closed


def test_fetch_configures_separate_connect_and_read_timeouts(monkeypatch):
    created = install(monkeypatch, FakeResponse())
    limits = RemoteLimits(connect_timeout=1.5, read_timeout=2.5)

    HttpClientTransport().fetch(URL, limits=limits)

    connection = created[0]
    assert connection.timeout == 1.5
    assert connection.sock.timeouts == [2.5]


def test_fetch_uses_a_verified_default_tls_context(monkeypatch):
    created = install(monkeypatch, FakeResponse())

    HttpClientTransport().fetch(URL, limits=RemoteLimits())

    context = created[0].context
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_fetch_keeps_bytes_exactly_at_the_response_limit(monkeypatch):
    response = FakeResponse(body=b"12345678")
    install(monkeypatch, response)

    result = HttpClientTransport().fetch(URL, limits=RemoteLimits(response_bytes=8))

    assert result.content == b"12345678"
    assert response.closed


def test_fetch_rejects_bytes_above_the_response_limit(monkeypatch):
    response = FakeResponse(body=b"123456789")
    created = install(monkeypatch, response)

    with pytest.raises(RemoteTransportError, match="response_bytes=8"):
        HttpClientTransport().fetch(URL, limits=RemoteLimits(response_bytes=8))

    assert response.closed
    assert created[0].closed


def test_fetch_returns_a_redirect_without_requesting_its_target(monkeypatch):
    response = FakeResponse(status=302, headers=[("Location", "https://other.example/b.xsd")])
    created = install(monkeypatch, response)

    result = HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert result.status == 302
    assert result.headers["Location"] == "https://other.example/b.xsd"
    assert created[0].requests == [("GET", "/a/main.xsd")]
    assert response.closed
    assert created[0].closed


@pytest.mark.parametrize(
    "url",
    [
        "http://schemas.example/a/main.xsd",
        "file:///etc/passwd",
        "ftp://schemas.example/a/main.xsd",
        "schemas.example/a/main.xsd",
    ],
)
def test_fetch_rejects_non_https_urls_before_connecting(monkeypatch, url):
    created = install(monkeypatch, FakeResponse())

    with pytest.raises(RemoteTransportError, match="https"):
        HttpClientTransport().fetch(url, limits=RemoteLimits())

    assert created == []


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https:///a/main.xsd",
        "https://schemas.example:not-a-port/a/main.xsd",
    ],
)
def test_fetch_rejects_urls_without_a_valid_host_or_port(monkeypatch, url):
    created = install(monkeypatch, FakeResponse())

    with pytest.raises(RemoteTransportError):
        HttpClientTransport().fetch(url, limits=RemoteLimits())

    assert created == []


def test_fetch_propagates_a_non_success_status(monkeypatch):
    response = FakeResponse(status=404, body=b"missing")
    created = install(monkeypatch, response)

    result = HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert result.status == 404
    assert result.content == b"missing"
    assert created[0].closed


def test_fetch_preserves_the_query_and_drops_the_fragment(monkeypatch):
    created = install(monkeypatch, FakeResponse())
    url = "https://schemas.example/a/main.xsd?version=2#fragment"

    result = HttpClientTransport().fetch(url, limits=RemoteLimits())

    assert created[0].requests == [("GET", "/a/main.xsd?version=2")]
    assert result.url == url


def test_fetch_requests_the_root_for_an_empty_path(monkeypatch):
    created = install(monkeypatch, FakeResponse())

    HttpClientTransport().fetch("https://schemas.example", limits=RemoteLimits())

    assert created[0].requests == [("GET", "/")]


def test_fetch_uses_an_explicit_port(monkeypatch):
    created = install(monkeypatch, FakeResponse())

    HttpClientTransport().fetch("https://schemas.example:8443/x", limits=RemoteLimits())

    assert created[0].port == 8443


def test_response_headers_are_case_insensitive(monkeypatch):
    response = FakeResponse(
        status=307,
        headers=[("Content-Type", "application/xml"), ("LOCATION", "https://other.example/b.xsd")],
    )
    install(monkeypatch, response)

    result = HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert result.headers["content-type"] == "application/xml"
    assert result.headers["location"] == "https://other.example/b.xsd"
    assert result.headers["Location"] == "https://other.example/b.xsd"
    assert list(result.headers) == ["Content-Type", "LOCATION"]
    assert len(result.headers) == 2
    assert "missing" not in result.headers
    with pytest.raises(KeyError):
        result.headers["missing"]


def test_fetch_wraps_a_malformed_status_line(monkeypatch):
    failure = http.client.BadStatusLine("not-http")
    created = install(monkeypatch, FakeResponse(), getresponse_error=failure)

    with pytest.raises(RemoteTransportError, match="malformed") as caught:
        HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert caught.value.__cause__ is failure
    assert created[0].closed


def test_fetch_closes_the_response_after_a_read_error(monkeypatch):
    failure = http.client.IncompleteRead(b"<xs:sc", 20)
    response = FakeResponse(body=b"<xs:sc", read_error=failure)
    created = install(monkeypatch, response)

    with pytest.raises(RemoteTransportError, match="malformed") as caught:
        HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert caught.value.__cause__ is failure
    assert response.closed
    assert created[0].closed


def test_fetch_closes_the_connection_when_connecting_fails(monkeypatch):
    failure = OSError("connection refused")
    created = install(monkeypatch, FakeResponse(), connect_error=failure)

    with pytest.raises(OSError) as caught:
        HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert caught.value is failure
    assert created[0].closed


def test_remote_limits_defaults_are_finite_and_exact():
    limits = RemoteLimits()

    assert limits.response_bytes == 16 * 1024 * 1024
    assert limits.closure_bytes == 128 * 1024 * 1024
    assert limits.resources == 128
    assert limits.depth == 16
    assert limits.redirects == 5
    assert limits.connect_timeout == 10
    assert limits.read_timeout == 30
    assert limits.xml_depth == 256
    assert limits.xml_elements == 250_000


@pytest.mark.parametrize(
    "name",
    [
        "response_bytes",
        "closure_bytes",
        "resources",
        "depth",
        "redirects",
        "connect_timeout",
        "read_timeout",
        "xml_depth",
        "xml_elements",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True])
def test_remote_limits_reject_nonpositive_budgets(name, value):
    with pytest.raises(ValueError, match=name):
        RemoteLimits(**{name: value})


@pytest.mark.parametrize("name", ["connect_timeout", "read_timeout"])
@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_remote_limits_reject_nonfinite_timeouts(name, value):
    with pytest.raises(ValueError, match=name):
        RemoteLimits(**{name: value})
