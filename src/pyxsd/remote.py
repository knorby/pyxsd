"""Bounded, single-request HTTPS retrieval for remote XML resources.

This module is deliberately only the transport seam. It does not decide
whether a URL may be retrieved, does not follow redirects, and is not wired
into schema loading: :meth:`RemoteTransport.fetch` performs exactly one HTTP
request and returns a redirect response, headers included, so the caller's
policy can authorize the next hop before asking for it.

Address and origin policy also stay with the caller. A policy that resolves a
hostname before calling :meth:`fetch` and then lets the transport resolve it
again cannot claim DNS-rebinding immunity: the name may resolve to a different
address for the connection. This transport does not expose the connected peer
address, so it cannot close that gap on its own, and the built-in policy must
not describe itself as a complete SSRF sandbox.
"""

from __future__ import annotations

import http.client
import io
import math
import socket
import ssl
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from pyxsd.exceptions import PyXSDError

#: The largest single response read; the body is read incrementally so the
#: total request deadline is checked between reads.
_READ_CHUNK = 64 * 1024


class RemoteTransportError(PyXSDError):
    """A bounded remote fetch failed; no response is available."""


@dataclass(frozen=True)
class RemoteLimits:
    """Finite budgets for remote retrieval and the XML it admits.

    ``response_bytes`` bounds one downloaded resource and ``closure_bytes``
    bounds all resources fetched for one compilation. ``resources`` and
    ``depth`` bound the dependency closure, ``redirects`` bounds the hops the
    caller may authorize, and the three timeouts configure connecting, each
    socket read, and the total request. ``xml_depth`` and ``xml_elements`` are
    structural limits for the strict parser that consumes a downloaded
    resource.
    """

    response_bytes: int = 16 * 1024 * 1024
    closure_bytes: int = 128 * 1024 * 1024
    resources: int = 128
    depth: int = 16
    redirects: int = 5
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    request_seconds: float = 30.0
    xml_depth: int = 256
    xml_elements: int = 250_000

    def __post_init__(self) -> None:
        for name, value in (
            ("response_bytes", self.response_bytes),
            ("closure_bytes", self.closure_bytes),
            ("resources", self.resources),
            ("depth", self.depth),
            ("redirects", self.redirects),
            ("xml_depth", self.xml_depth),
            ("xml_elements", self.xml_elements),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.response_bytes > self.closure_bytes:
            raise ValueError("response_bytes must not exceed closure_bytes")
        for name, timeout in (
            ("connect_timeout", self.connect_timeout),
            ("read_timeout", self.read_timeout),
            ("request_seconds", self.request_seconds),
        ):
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number of seconds")


class _Headers(Mapping[str, str]):
    """A case-insensitive, first-value-wins view of one response's headers."""

    __slots__ = ("_fields",)

    def __init__(self, fields: Iterable[tuple[str, str]]) -> None:
        self._fields = tuple(fields)

    def __getitem__(self, name: str) -> str:
        folded = name.lower()
        for field, value in self._fields:
            if field.lower() == folded:
                return value
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        return (field for field, _ in self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    def _normalized(self) -> tuple[tuple[str, str], ...]:
        return tuple((field.lower(), value) for field, value in self._fields)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _Headers):
            return self._normalized() == other._normalized()
        if isinstance(other, Mapping):
            return self._normalized() == tuple(
                (str(name).lower(), value) for name, value in other.items()
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._normalized())


@dataclass(frozen=True)
class RemoteResponse:
    """One HTTP response, bounded and exactly as received.

    ``headers`` keeps HTTP field-name case-insensitivity, so a redirect's
    ``Location`` is found however the server cased it.
    """

    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes


def _deadline_error(url: str, request_seconds: float) -> RemoteTransportError:
    return RemoteTransportError(
        f"the request to '{url}' exceeded request_seconds={request_seconds}"
    )


class _DeadlineSocket:
    """A socket proxy that re-arms the request deadline before every read.

    ``http.client`` reads status lines, header lines, chunk framing, and body
    data through a buffered file object. Capping a high-level ``readline`` or
    ``read1`` call is not enough: one call may issue many underlying reads.
    The proxy recomputes the remaining request time before every
    ``recv_into`` and ``sendall`` and installs ``min(read_timeout, remaining)``
    on the real socket, so every low-level read is bounded by both limits.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        clock: Callable[[], float],
        deadline: float,
        read_timeout: float,
        url: str,
        request_seconds: float,
    ) -> None:
        self._sock = sock
        self._clock = clock
        self._deadline = deadline
        self._read_timeout = read_timeout
        self._url = url
        self._request_seconds = request_seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)

    def _next_timeout(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise _deadline_error(self._url, self._request_seconds)
        return min(self._read_timeout, remaining)

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        self._sock.settimeout(self._next_timeout())
        return self._sock.recv_into(buffer, nbytes, flags)

    def sendall(self, data: Any, flags: int = 0) -> None:
        self._sock.settimeout(self._next_timeout())
        self._sock.sendall(data, flags)

    def makefile(
        self,
        mode: Literal["rb"] = "rb",
        buffering: int | None = None,
    ) -> io.BufferedReader:
        raw = socket.SocketIO(cast(socket.socket, self), mode)
        # ``socket.makefile`` counts every file object so that ``socket.close``
        # defers the real close until the last file is closed; ``SocketIO.close``
        # performs the matching decrement through ``_decref_socketios``.
        raw_socket = cast(Any, self._sock)
        raw_socket._io_refs += 1
        return io.BufferedReader(raw)


class RemoteTransport(Protocol):
    """The retrieval seam a remote resource policy calls one hop at a time."""

    def fetch(self, url: str, *, limits: RemoteLimits) -> RemoteResponse:
        """Perform one HTTPS request without following a redirect."""
        ...


class HttpClientTransport:
    """The stdlib HTTPS transport used when a policy opts into remote access.

    Each call creates a fresh default TLS context (certificate and hostname
    verification enabled), connects with at most ``connect_timeout``, then
    reads the response through a deadline-aware socket proxy. Every low-level
    socket read, including the status line, each header line, chunk framing,
    and body data, re-arms the timeout to the smaller of ``read_timeout`` and
    the time left before ``request_seconds``. The deadline therefore covers
    connecting, the response headers, and all body reads, so a server that
    drips bytes cannot hold the request open indefinitely.
    ``Content-Length`` is never trusted; a response whose streamed bytes exceed
    the budget is rejected. The response and connection are closed on every
    outcome.

    Failures that leave no usable response raise :class:`RemoteTransportError`:
    a non-HTTPS URL, a missing host or invalid port, an oversized response, a
    malformed HTTP response, or an expired ``request_seconds`` deadline. DNS,
    TLS, and socket failures (including a read timeout shorter than the
    remaining deadline) propagate as the original :class:`OSError` subclass.
    Either way the connection is already closed when the exception escapes.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock

    def fetch(self, url: str, *, limits: RemoteLimits) -> RemoteResponse:
        host, port, target = _request_target(url)
        deadline = self._clock() + limits.request_seconds
        remaining = self._remaining(url, limits, deadline)
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=min(limits.connect_timeout, remaining),
            context=ssl.create_default_context(),
        )
        response: http.client.HTTPResponse | None = None
        try:
            connection.connect()
            raw_sock = cast(socket.socket, connection.sock)
            connection.sock = cast(
                socket.socket,
                _DeadlineSocket(
                    raw_sock,
                    clock=self._clock,
                    deadline=deadline,
                    read_timeout=limits.read_timeout,
                    url=url,
                    request_seconds=limits.request_seconds,
                ),
            )
            self._remaining(url, limits, deadline)
            connection.request("GET", target)
            response = connection.getresponse()
            chunks: list[bytes] = []
            total = 0
            while True:
                self._remaining(url, limits, deadline)
                chunk = response.read1(min(_READ_CHUNK, limits.response_bytes + 1 - total))
                if not chunk:
                    self._remaining(url, limits, deadline)
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limits.response_bytes:
                    raise RemoteTransportError(
                        f"the response from '{url}' exceeds response_bytes={limits.response_bytes}"
                    )
            return RemoteResponse(
                url=url,
                status=response.status,
                headers=_Headers(response.getheaders()),
                content=b"".join(chunks),
            )
        except http.client.HTTPException as exc:
            raise RemoteTransportError(f"malformed HTTP response from '{url}': {exc}") from exc
        except TimeoutError as exc:
            if self._clock() >= deadline:
                raise _deadline_error(url, limits.request_seconds) from exc
            raise
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()

    def _remaining(self, url: str, limits: RemoteLimits, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _deadline_error(url, limits.request_seconds)
        return remaining


def _request_target(url: str) -> tuple[str, int, str]:
    """Split a URL into connection coordinates and an HTTP request target."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise RemoteTransportError(
            f"the remote URL '{url}' is not https; only https retrieval is supported"
        )
    if not parsed.hostname:
        raise RemoteTransportError(f"the remote URL '{url}' has no host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RemoteTransportError(f"the remote URL '{url}' has an invalid port") from exc
    if port == 0:
        raise RemoteTransportError(f"the remote URL '{url}' has an invalid port")
    return (
        parsed.hostname,
        443 if port is None else port,
        urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
    )


__all__ = [
    "HttpClientTransport",
    "RemoteLimits",
    "RemoteResponse",
    "RemoteTransport",
    "RemoteTransportError",
]
