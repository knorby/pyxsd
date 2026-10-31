"""Bounded HTTPS transport behavior, exercised through fake connections.

Nothing in this module creates a socket: every test replaces
``http.client.HTTPSConnection`` with a fake and inspects the request the
transport built. Redirects are returned, never followed, so the caller's
policy remains the only place that authorizes a next hop.
"""

import http.client
import socket
import ssl
import threading

import pytest

from pyxsd.remote import (
    HttpClientTransport,
    RemoteLimits,
    RemoteResponse,
    RemoteTransportError,
    _DeadlineSocket,
)

URL = "https://schemas.example/a/main.xsd"

STATUS_LINE = b"HTTP/1.1 200 OK\r\n"
FIXED_HEADERS = b"Content-Type: application/xml\r\nContent-Length: 4\r\n\r\n"
FIXED_RESPONSE = STATUS_LINE + FIXED_HEADERS + b"body"
CHUNK_PREFIX = STATUS_LINE + b"Transfer-Encoding: chunked\r\n\r\n"
CHUNK_SIZE_LINE = b"5;ext=1\r\n"
CHUNKED_RESPONSE = CHUNK_PREFIX + CHUNK_SIZE_LINE + b"hello\r\n0\r\n\r\n"


class FakeClock:
    """A deterministic monotonic clock; tests advance it explicitly."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSocket:
    """Records the socket read timeout installed after connecting."""

    def __init__(self) -> None:
        self.timeouts = []

    def settimeout(self, value) -> None:
        self.timeouts.append(value)


class ScriptedSocket:
    """A finite byte stream whose low-level reads advance a fake clock.

    ``slow_after`` is the payload offset at which each receive begins to cost
    ``recv_delay`` seconds; bytes before it arrive instantly. ``recv_chunk``
    caps one receive so a test can drip one byte at a time.
    """

    def __init__(
        self,
        payload: bytes,
        *,
        clock: FakeClock,
        recv_delay: float = 0.0,
        slow_after: int = 0,
        recv_chunk: int = 1,
    ) -> None:
        self.payload = payload
        self.offset = 0
        self.timeouts = []
        self.closed = False
        self._io_refs = 0
        self._clock = clock
        self._recv_delay = recv_delay
        self._slow_after = slow_after
        self._recv_chunk = recv_chunk

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        if self.offset >= self._slow_after:
            self._clock.advance(self._recv_delay)
        if self.offset >= len(self.payload):
            return 0
        size = nbytes or len(buffer)
        size = min(size, self._recv_chunk, len(self.payload) - self.offset)
        buffer[:size] = self.payload[self.offset : self.offset + size]
        self.offset += size
        return size

    def settimeout(self, value) -> None:
        self.timeouts.append(value)

    def gettimeout(self):
        return None

    def sendall(self, data, flags: int = 0) -> None:
        pass

    def _decref_socketios(self) -> None:
        self._io_refs -= 1

    def _inc_ref(self) -> None:
        self._io_refs += 1

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    """A minimal ``http.client.HTTPResponse`` with bounded reads."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers=(),
        body: bytes = b"",
        read_error: Exception | None = None,
        clock: FakeClock | None = None,
        read_delay: float = 0.0,
    ) -> None:
        self.status = status
        self._headers = list(headers)
        self._body = body
        self._offset = 0
        self._read_error = read_error
        self._clock = clock
        self._read_delay = read_delay
        self.read_sizes = []
        self.closed = False

    def getheaders(self):
        return list(self._headers)

    def read1(self, amount: int) -> bytes:
        self.read_sizes.append(amount)
        if self._clock is not None:
            self._clock.advance(self._read_delay)
        if self._read_error is not None:
            raise self._read_error
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def install(
    monkeypatch,
    response: FakeResponse,
    *,
    connect_error=None,
    getresponse_error=None,
    clock: FakeClock | None = None,
    connect_delay: float = 0.0,
):
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
            if clock is not None:
                clock.advance(connect_delay)
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


def install_scripted(
    monkeypatch,
    payload: bytes,
    *,
    clock: FakeClock,
    recv_delay: float = 0.0,
    slow_after: int = 0,
    recv_chunk: int = 1,
):
    """Replace ``HTTPSConnection`` with one that serves a real HTTPResponse."""
    created = []

    class FakeConnection:
        def __init__(self, host, port=None, *, timeout=None, context=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.context = context
            self.requests = []
            self.sock = None
            self.raw = None
            self.closed = False
            created.append(self)

        def connect(self) -> None:
            self.raw = ScriptedSocket(
                payload,
                clock=clock,
                recv_delay=recv_delay,
                slow_after=slow_after,
                recv_chunk=recv_chunk,
            )
            self.sock = self.raw

        def request(self, method: str, target: str) -> None:
            self.requests.append((method, target))
            self.sock.sendall(b"GET " + target.encode("ascii") + b" HTTP/1.1\r\n\r\n")

        def getresponse(self) -> http.client.HTTPResponse:
            response = http.client.HTTPResponse(self.sock, method="GET")
            response.begin()
            return response

        def close(self) -> None:
            self.closed = True
            if self.sock is not None:
                self.sock.close()

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
    assert response.read_sizes
    assert all(size <= limits.response_bytes + 1 for size in response.read_sizes)
    assert response.closed
    assert connection.closed


def test_fetch_configures_separate_connect_and_read_timeouts(monkeypatch):
    clock = FakeClock()
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    created = install_scripted(monkeypatch, payload, clock=clock)
    limits = RemoteLimits(connect_timeout=1.5, read_timeout=2.5)

    result = HttpClientTransport(clock=clock).fetch(URL, limits=limits)

    assert created[0].timeout == 1.5
    assert created[0].raw.timeouts
    assert set(created[0].raw.timeouts) == {2.5}
    assert result.content == b""


def test_fetch_uses_a_verified_default_tls_context(monkeypatch):
    created = install(monkeypatch, FakeResponse())

    HttpClientTransport().fetch(URL, limits=RemoteLimits())

    context = created[0].context
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_fetch_clamps_connect_timeout_to_the_request_deadline(monkeypatch):
    clock = FakeClock(100.0)
    created = install(monkeypatch, FakeResponse(), clock=clock)

    HttpClientTransport(clock=clock).fetch(
        URL, limits=RemoteLimits(connect_timeout=10, request_seconds=4)
    )

    assert created[0].timeout == 4


def test_fetch_stops_when_the_deadline_expires_while_connecting(monkeypatch):
    clock = FakeClock()
    created = install(monkeypatch, FakeResponse(), clock=clock, connect_delay=5)

    with pytest.raises(RemoteTransportError, match="request_seconds=5"):
        HttpClientTransport(clock=clock).fetch(
            URL, limits=RemoteLimits(connect_timeout=10, request_seconds=5)
        )

    assert created[0].closed


def test_fetch_enforces_the_deadline_inside_the_status_line(monkeypatch):
    clock = FakeClock()
    created = install_scripted(monkeypatch, FIXED_RESPONSE, clock=clock, recv_delay=1)

    with pytest.raises(RemoteTransportError, match="request_seconds=2"):
        HttpClientTransport(clock=clock).fetch(URL, limits=RemoteLimits(request_seconds=2))

    assert created[0].raw.offset < len(STATUS_LINE)
    assert created[0].closed


def test_fetch_enforces_the_deadline_inside_a_header_line(monkeypatch):
    clock = FakeClock()
    created = install_scripted(
        monkeypatch,
        FIXED_RESPONSE,
        clock=clock,
        recv_delay=1,
        slow_after=len(STATUS_LINE),
    )

    with pytest.raises(RemoteTransportError, match="request_seconds=2"):
        HttpClientTransport(clock=clock).fetch(URL, limits=RemoteLimits(request_seconds=2))

    headers_end = len(STATUS_LINE) + len(FIXED_HEADERS)
    assert len(STATUS_LINE) < created[0].raw.offset < headers_end
    assert created[0].closed


def test_fetch_enforces_the_deadline_inside_a_chunk_size_line(monkeypatch):
    clock = FakeClock()
    created = install_scripted(
        monkeypatch,
        CHUNKED_RESPONSE,
        clock=clock,
        recv_delay=1,
        slow_after=len(CHUNK_PREFIX),
    )

    with pytest.raises(RemoteTransportError, match="request_seconds=2"):
        HttpClientTransport(clock=clock).fetch(URL, limits=RemoteLimits(request_seconds=2))

    chunk_line_end = len(CHUNK_PREFIX) + len(CHUNK_SIZE_LINE)
    assert len(CHUNK_PREFIX) < created[0].raw.offset < chunk_line_end
    assert created[0].closed


def test_fetch_enforces_the_deadline_inside_the_body(monkeypatch):
    clock = FakeClock()
    headers_end = len(STATUS_LINE) + len(FIXED_HEADERS)
    created = install_scripted(
        monkeypatch,
        FIXED_RESPONSE,
        clock=clock,
        recv_delay=1,
        slow_after=headers_end,
    )

    with pytest.raises(RemoteTransportError, match="request_seconds=2"):
        HttpClientTransport(clock=clock).fetch(URL, limits=RemoteLimits(request_seconds=2))

    assert headers_end < created[0].raw.offset < len(FIXED_RESPONSE)
    assert created[0].closed


def test_deadline_socket_keeps_the_fd_alive_until_its_file_closes():
    reader, writer = socket.socketpair()
    clock = FakeClock()
    wrapped = _DeadlineSocket(
        reader,
        clock=clock,
        deadline=30.0,
        read_timeout=30.0,
        url=URL,
        request_seconds=30.0,
    )
    stream = wrapped.makefile("rb")
    payload = b"x" * (100 * 1024)
    errors = []

    def send() -> None:
        try:
            writer.sendall(payload)
        except OSError as exc:
            errors.append(exc)
        finally:
            writer.close()

    thread = threading.Thread(target=send)
    thread.start()
    try:
        wrapped.close()
        assert stream.read() == payload
    finally:
        stream.close()
        wrapped.close()
        thread.join(timeout=5)
    assert not errors


def test_fetch_reports_a_deadline_timeout_as_a_transport_error(monkeypatch):
    clock = FakeClock()
    failure = TimeoutError("timed out")
    response = FakeResponse(read_error=failure, clock=clock, read_delay=5)
    created = install(monkeypatch, response, clock=clock)

    with pytest.raises(RemoteTransportError, match="request_seconds=5") as caught:
        HttpClientTransport(clock=clock).fetch(
            URL, limits=RemoteLimits(read_timeout=30, request_seconds=5)
        )

    assert caught.value.__cause__ is failure
    assert response.closed
    assert created[0].closed


def test_fetch_propagates_a_read_timeout_before_the_deadline(monkeypatch):
    clock = FakeClock()
    failure = TimeoutError("timed out")
    response = FakeResponse(read_error=failure, clock=clock, read_delay=1)
    created = install(monkeypatch, response, clock=clock)

    with pytest.raises(TimeoutError) as caught:
        HttpClientTransport(clock=clock).fetch(
            URL, limits=RemoteLimits(read_timeout=2.5, request_seconds=30)
        )

    assert caught.value is failure
    assert response.closed
    assert created[0].closed


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
        "https://schemas.example:0/a/main.xsd",
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


def test_remote_response_has_value_equality_and_is_hashable(monkeypatch):
    headers = [("Content-Type", "application/xml")]
    install(monkeypatch, FakeResponse(headers=headers, body=b"<xs:schema/>"))
    first = HttpClientTransport().fetch(URL, limits=RemoteLimits())
    install(monkeypatch, FakeResponse(headers=headers, body=b"<xs:schema/>"))
    second = HttpClientTransport().fetch(URL, limits=RemoteLimits())

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1
    assert first.headers == {"content-type": "application/xml"}
    assert first.headers != object()
    assert first != RemoteResponse(url=URL, status=200, headers=dict(headers), content=b"other")


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
    assert limits.request_seconds == 30
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
        "request_seconds",
        "xml_depth",
        "xml_elements",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True])
def test_remote_limits_reject_nonpositive_budgets(name, value):
    with pytest.raises(ValueError, match=name):
        RemoteLimits(**{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "response_bytes",
        "closure_bytes",
        "resources",
        "depth",
        "redirects",
        "xml_depth",
        "xml_elements",
    ],
)
@pytest.mark.parametrize("value", [1.0, "16"])
def test_remote_limits_reject_non_integer_budgets(name, value):
    with pytest.raises(ValueError, match=name):
        RemoteLimits(**{name: value})


@pytest.mark.parametrize("name", ["connect_timeout", "read_timeout", "request_seconds"])
@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_remote_limits_reject_nonfinite_timeouts(name, value):
    with pytest.raises(ValueError, match=name):
        RemoteLimits(**{name: value})


@pytest.mark.parametrize("name", ["connect_timeout", "read_timeout", "request_seconds"])
@pytest.mark.parametrize("value", ["30", None, object()])
def test_remote_limits_reject_nonnumeric_timeouts(name, value):
    with pytest.raises(ValueError, match=name):
        RemoteLimits(**{name: value})


def test_remote_limits_require_response_bytes_within_closure_bytes():
    equal = RemoteLimits(response_bytes=16, closure_bytes=16)
    assert equal.response_bytes == equal.closure_bytes == 16

    with pytest.raises(ValueError, match="response_bytes"):
        RemoteLimits(response_bytes=17, closure_bytes=16)
