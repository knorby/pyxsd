"""Bounded, opt-in HTTPS retrieval for remote XML resources.

:meth:`RemoteTransport.fetch` is only the transport seam: it performs exactly
one HTTP request and returns a redirect response, headers included, so the
caller's policy authorizes the next hop before asking for it.
:class:`RemoteResourcePolicy` is that policy. It is never selected implicitly:
remote access is disabled unless an application explicitly passes a policy to
:func:`pyxsd.compile` or :func:`pyxsd.parse`. Selecting the policy authorizes
its remote root schema; same-origin dependencies need no further entry, while
cross-origin dependencies require :attr:`RemoteResourcePolicy.allowed_origins`
and instance-driven hints require
:attr:`RemoteResourcePolicy.allow_remote_hints`.

Address and origin policy stay with the policy, not the transport. The policy
resolves a hostname before calling :meth:`fetch`, and the transport then
resolves it again for the connection, so a name can be rebound between the two
lookups (DNS rebinding). The transport does not expose the connected peer
address, so this policy must not be described as a complete SSRF sandbox; it is
one defense among others and denies loopback, private, link-local, and other
non-global addresses by default.

A policy instance owns one retrieval closure: unique canonical URLs, redirect
hops, per-resource bytes, and total closure bytes are accounted on it. There is
no persistent cache; repeated reads are fetched and charged again.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import math
import socket
import ssl
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol, cast
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.resource_policy import (
    ResourceData,
    ResourcePolicyError,
    ResourcePurpose,
    validate_purpose,
)
from pyxsd.resource_refs import classify_resource, resolve_reference
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

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


#: Resolve a hostname and port to the addresses a connection could use.
AddressResolver = Callable[[str, int], Sequence[str]]

#: An HTTPS origin: (scheme, lowercase host, effective port).
_Origin = tuple[str, str, int]


def _system_addresses(host: str, port: int) -> Sequence[str]:
    """The system resolver's addresses for *host*, with no caching."""
    addresses: list[str] = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sockaddr = cast(tuple[str, int], info[4])
        addresses.append(sockaddr[0])
    return addresses


def _url_parts(url: str) -> tuple[str, str, int, str, str]:
    """Split an HTTPS URL into (scheme, host, effective port, path, query)."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme != "https":
        raise ResourcePolicyError(
            f"the remote URL '{url}' is not https; only https retrieval is supported"
        )
    host = parsed.hostname
    if not host:
        raise ResourcePolicyError(f"the remote URL '{url}' has no host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ResourcePolicyError(f"the remote URL '{url}' has an invalid port") from exc
    if port == 0:
        raise ResourcePolicyError(f"the remote URL '{url}' has an invalid port")
    return scheme, host, 443 if port is None else port, parsed.path or "/", parsed.query


def _canonical_url(url: str) -> str:
    """Canonicalize *url* for identity: lowercase host, no default port or fragment."""
    scheme, host, port, path, query = _url_parts(url)
    display = f"[{host}]" if ":" in host else host
    netloc = display if port == 443 else f"{display}:{port}"
    return urlunsplit((scheme, netloc, path, query, ""))


def _origin(url: str) -> _Origin:
    """The HTTPS origin of *url*."""
    scheme, host, port, _, _ = _url_parts(url)
    return scheme, host, port


def _allowed_origin(origin: str) -> _Origin:
    """Parse one ``allowed_origins`` entry, rejecting anything but an origin."""
    parsed = urlsplit(origin)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"allowed_origins entry {origin!r} is not an https origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"allowed_origins entry {origin!r} has an invalid port") from exc
    if port == 0:
        raise ValueError(f"allowed_origins entry {origin!r} has an invalid port")
    return "https", parsed.hostname.lower(), 443 if port is None else port


class RemoteResourcePolicy:
    """Opt-in HTTPS schema loading with one closure's budgets and trust rules.

    Selecting the policy authorizes the application's remote root schema.
    Dependencies of an admitted remote document may reference the same origin
    as the root or the requesting document; anything else must appear in
    ``allowed_origins``. An instance ``xsi:schemaLocation`` /
    ``xsi:noNamespaceSchemaLocation`` that names a remote schema needs
    ``allow_remote_hints=True`` as well. A local schema (or anonymous stream)
    may reference a remote dependency only with ``allow_local_remote=True``.

    Every remote read is HTTPS, address-checked, and budgeted: one response's
    bytes never exceed ``limits.response_bytes``, the whole closure never
    exceeds ``limits.closure_bytes``, ``limits.resources`` unique URLs, and
    ``limits.depth`` dependency levels. Redirects are followed one request at
    a time, with the scheme, origin, and addresses revalidated before each new
    request, up to ``limits.redirects`` hops.

    Pivots are denied: a remote document may never resolve to a local path or
    a ``file:``/other non-HTTPS URL, remote instance documents are refused, and
    a remote schema never loads a Python overlay.

    ``parse`` always uses :meth:`XmlParserPolicy.strict` and the policy's XML
    limits (``response_bytes`` for bytes, ``xml_depth`` and ``xml_elements``
    for structure), so a downloaded document declaring a DTD or entity is
    denied. There is no persistent cache and no external-entity handler.

    ``resolver`` is consulted before every request. The transport resolves the
    hostname again for the connection, so this preconnection check cannot
    claim DNS-rebinding immunity (see the module docstring).

    ``limits`` defaults to a fresh :class:`RemoteLimits`; ``transport``
    defaults to :class:`HttpClientTransport`; ``resolver`` defaults to the
    system resolver.
    """

    limits: RemoteLimits
    allowed_origins: frozenset[_Origin]
    allow_remote_hints: bool
    allow_local_remote: bool
    allow_private_addresses: bool
    _resources: set[str]
    _closure_bytes: int
    _redirects: int
    _depths: dict[str, int]
    _root_origin: _Origin | None

    def __init__(
        self,
        *,
        allowed_origins: Iterable[str] = (),
        allow_remote_hints: bool = False,
        allow_local_remote: bool = False,
        allow_private_addresses: bool = False,
        limits: RemoteLimits | None = None,
        transport: RemoteTransport | None = None,
        resolver: AddressResolver | None = None,
    ) -> None:
        self.limits = RemoteLimits() if limits is None else limits
        self.allowed_origins = frozenset(_allowed_origin(origin) for origin in allowed_origins)
        self.allow_remote_hints = allow_remote_hints
        self.allow_local_remote = allow_local_remote
        self.allow_private_addresses = allow_private_addresses
        self._transport: RemoteTransport = HttpClientTransport() if transport is None else transport
        self._resolver: AddressResolver = _system_addresses if resolver is None else resolver
        self._reset_closure()

    def read(self, source: str, *, base: str | None, purpose: ResourcePurpose) -> ResourceData:
        """Read a local resource or an authorized remote schema."""
        checked = validate_purpose(purpose)
        if base is None and checked != "instance":
            # A caller-supplied schema starts a new retrieval closure, so one
            # policy instance can serve several compilations.
            self._reset_closure()
        reference = resolve_reference(source, base=base)
        classified = classify_resource(reference)
        if (
            base is not None
            and classified.kind != "remote"
            and classify_resource(base).kind == "remote"
        ):
            raise ResourcePolicyError(
                f"the remote document '{base}' cannot pivot to the local resource "
                f"'{classified.value}'"
            )
        if classified.kind == "remote":
            return self._read_remote(classified.value, base=base, purpose=checked)
        return self._read_local(classified.value)

    def parse(self, data: ResourceData, context: NamespaceContext) -> ET.Element:
        """Parse admitted bytes with the strict defused profile and XML limits."""
        return parse_with_namespaces(
            io.BytesIO(data.content),
            context,
            policy=XmlParserPolicy.strict(),
            limits=XmlLimits(
                max_bytes=self.limits.response_bytes,
                max_depth=self.limits.xml_depth,
                max_elements=self.limits.xml_elements,
            ),
        )

    def _read_local(self, reference: str) -> ResourceData:
        path = Path(reference)
        return ResourceData(str(path), path.read_bytes())

    def _read_remote(self, url: str, *, base: str | None, purpose: ResourcePurpose) -> ResourceData:
        canonical = _canonical_url(url)
        origin = _origin(canonical)
        if purpose == "instance":
            raise ResourcePolicyError(
                f"the remote instance '{canonical}' is not supported; "
                "remote loading is limited to schemas"
            )
        if purpose == "schema-hint" and not self.allow_remote_hints:
            raise ResourcePolicyError(
                f"the instance schema hint '{canonical}' is remote; "
                "set allow_remote_hints=True to permit instance-driven remote schemas"
            )
        is_root = base is None
        depth = 0
        if base is not None:
            base_ref = classify_resource(base)
            if base_ref.kind == "remote":
                parent = _canonical_url(base_ref.value)
                depth = self._depths.get(parent, 0) + 1
                parent_origin = _origin(parent)
                if not (
                    origin == parent_origin
                    or (self._root_origin is not None and origin == self._root_origin)
                    or origin in self.allowed_origins
                ):
                    raise ResourcePolicyError(
                        f"the remote dependency '{canonical}' is cross-origin; "
                        "add its origin to allowed_origins"
                    )
            elif not self.allow_local_remote:
                raise ResourcePolicyError(
                    f"the remote dependency '{canonical}' was referenced by a local "
                    "resource; set allow_local_remote=True to permit local-to-remote loading"
                )
        final_url, content = self._fetch(canonical, depth=depth)
        if is_root or self._root_origin is None:
            self._root_origin = _origin(final_url)
        self._depths.setdefault(final_url, depth)
        return ResourceData(final_url, content)

    def _fetch(self, url: str, *, depth: int) -> tuple[str, bytes]:
        """Fetch one resource, authorizing and revalidating each redirect hop."""
        current = url
        while True:
            self._admit(current, depth)
            response = self._transport.fetch(current, limits=self.limits)
            size = len(response.content)
            if size > self.limits.response_bytes:
                raise ResourcePolicyError(
                    f"the response from '{current}' exceeds "
                    f"response_bytes={self.limits.response_bytes}"
                )
            self._closure_bytes += size
            if self._closure_bytes > self.limits.closure_bytes:
                raise ResourcePolicyError(
                    f"the remote schema closure exceeds closure_bytes={self.limits.closure_bytes}"
                )
            status = response.status
            if 300 <= status < 400:
                location = response.headers.get("Location")
                if not location:
                    raise ResourcePolicyError(
                        f"the redirect response from '{current}' has no Location header"
                    )
                target = _canonical_url(urljoin(current, location))
                self._redirects += 1
                if self._redirects > self.limits.redirects:
                    raise ResourcePolicyError(
                        f"the redirect from '{current}' exceeds redirects={self.limits.redirects}"
                    )
                target_origin = _origin(target)
                if (
                    target_origin != _origin(current)
                    and target_origin not in self.allowed_origins
                    and target_origin != self._root_origin
                ):
                    raise ResourcePolicyError(
                        f"the redirect to '{target}' is cross-origin; "
                        "add its origin to allowed_origins"
                    )
                current = target
                continue
            if not 200 <= status < 300:
                raise ResourcePolicyError(
                    f"the remote resource '{current}' returned status {status}"
                )
            return current, response.content

    def _admit(self, url: str, depth: int) -> None:
        """Check address, depth, and resource-count budgets before a fetch."""
        if depth > self.limits.depth:
            raise ResourcePolicyError(
                f"the remote schema dependency depth exceeds depth={self.limits.depth}"
            )
        self._check_addresses(url)
        if url not in self._resources:
            if len(self._resources) >= self.limits.resources:
                raise ResourcePolicyError(
                    f"the remote schema closure exceeds resources={self.limits.resources}"
                )
            self._resources.add(url)
        self._depths.setdefault(url, depth)

    def _check_addresses(self, url: str) -> None:
        _, host, port, _, _ = _url_parts(url)
        try:
            addresses = self._resolver(host, port)
        except OSError as exc:
            raise ResourcePolicyError(
                f"the remote host '{host}' could not be resolved: {exc}"
            ) from exc
        if not addresses:
            raise ResourcePolicyError(f"the remote host '{host}' resolved to no addresses")
        if self.allow_private_addresses:
            return
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ResourcePolicyError(
                    f"the remote host '{host}' resolved to an invalid address '{address}'"
                ) from exc
            if not parsed.is_global:
                raise ResourcePolicyError(
                    f"the remote host '{host}' resolves to the non-public address "
                    f"'{address}'; set allow_private_addresses=True only for trusted hosts"
                )

    def _reset_closure(self) -> None:
        self._resources = set()
        self._closure_bytes = 0
        self._redirects = 0
        self._depths = {}
        self._root_origin = None


__all__ = [
    "AddressResolver",
    "HttpClientTransport",
    "RemoteLimits",
    "RemoteResourcePolicy",
    "RemoteResponse",
    "RemoteTransport",
    "RemoteTransportError",
]
