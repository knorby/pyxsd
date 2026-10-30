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
import math
import ssl
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from pyxsd.exceptions import PyXSDError

if TYPE_CHECKING:
    import socket


class RemoteTransportError(PyXSDError):
    """A bounded remote fetch failed; no response is available."""


@dataclass(frozen=True)
class RemoteLimits:
    """Finite budgets for remote retrieval and the XML it admits.

    ``response_bytes`` bounds one downloaded resource and ``closure_bytes``
    bounds all resources fetched for one compilation. ``resources`` and
    ``depth`` bound the dependency closure, ``redirects`` bounds the hops the
    caller may authorize, and the two timeouts configure connecting and then
    reading. ``xml_depth`` and ``xml_elements`` are structural limits for the
    strict parser that consumes a downloaded resource.
    """

    response_bytes: int = 16 * 1024 * 1024
    closure_bytes: int = 128 * 1024 * 1024
    resources: int = 128
    depth: int = 16
    redirects: int = 5
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
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
        for name, timeout in (
            ("connect_timeout", self.connect_timeout),
            ("read_timeout", self.read_timeout),
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


class RemoteTransport(Protocol):
    """The retrieval seam a remote resource policy calls one hop at a time."""

    def fetch(self, url: str, *, limits: RemoteLimits) -> RemoteResponse:
        """Perform one HTTPS request without following a redirect."""
        ...


class HttpClientTransport:
    """The stdlib HTTPS transport used when a policy opts into remote access.

    Each call creates a fresh default TLS context (certificate and hostname
    verification enabled), connects with ``connect_timeout``, raises the
    socket timeout to ``read_timeout``, and reads at most one byte beyond the
    response budget. ``Content-Length`` is never trusted; a response whose
    streamed bytes exceed the budget is rejected. The response and connection
    are closed on every outcome.
    """

    def fetch(self, url: str, *, limits: RemoteLimits) -> RemoteResponse:
        host, port, target = _request_target(url)
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=limits.connect_timeout,
            context=ssl.create_default_context(),
        )
        response: http.client.HTTPResponse | None = None
        try:
            connection.connect()
            sock = cast("socket.socket", connection.sock)
            sock.settimeout(limits.read_timeout)
            connection.request("GET", target)
            response = connection.getresponse()
            content = response.read(limits.response_bytes + 1)
            if len(content) > limits.response_bytes:
                raise RemoteTransportError(
                    f"the response from '{url}' exceeds response_bytes={limits.response_bytes}"
                )
            return RemoteResponse(
                url=url,
                status=response.status,
                headers=_Headers(response.getheaders()),
                content=content,
            )
        except http.client.HTTPException as exc:
            raise RemoteTransportError(f"malformed HTTP response from '{url}': {exc}") from exc
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()


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
