"""Opt-in remote schema loading: authorization, closure budgets, strict parsing.

Every test injects a fake transport and resolver; the autouse fixture below
turns any real socket use into a failure, so nothing here reaches the network.
"""

from __future__ import annotations

import io
import socket
from typing import Any

import pytest

import pyxsd
from pyxsd.namespaces import NamespaceContext
from pyxsd.remote import (
    HttpClientTransport,
    RemoteLimits,
    RemoteResourcePolicy,
    RemoteResponse,
    RemoteTransportError,
)
from pyxsd.resource_policy import (
    ResourceData,
    ResourcePolicyError,
    read_resource,
    validate_purpose,
)
from pyxsd.resource_refs import classify_resource, resolve_reference

XS = "http://www.w3.org/2001/XMLSchema"
ROOT = "https://a.example/root.xsd"
MID = "https://a.example/mid.xsd"
TYPES = "https://a.example/types.xsd"
FINAL = "https://a.example/real/root.xsd"
OTHER = "https://b.example/types.xsd"
GLOBAL_ADDRESS = "93.184.216.34"


def xsd(body: str = "", *, target: str | None = None) -> str:
    namespace = f' targetNamespace="{target}"' if target else ""
    return f'<xs:schema xmlns:xs="{XS}"{namespace}>{body}</xs:schema>'


SIMPLE_DOC = xsd('<xs:element name="root" type="xs:string"/>')
ROOT_DOC = xsd('<xs:include schemaLocation="types.xsd"/><xs:element name="root" type="Number"/>')
TYPES_DOC = xsd('<xs:simpleType name="Number"><xs:restriction base="xs:int"/></xs:simpleType>')
MID_DOC = xsd('<xs:include schemaLocation="types.xsd"/>')
IMPORT_ROOT_DOC = (
    f'<xs:schema xmlns:xs="{XS}" xmlns:t="urn:types" targetNamespace="urn:root">'
    '<xs:import namespace="urn:types" schemaLocation="types.xsd"/>'
    '<xs:element name="root" type="t:Number"/></xs:schema>'
)
IMPORT_TYPES_DOC = xsd(
    '<xs:simpleType name="Number"><xs:restriction base="xs:int"/></xs:simpleType>',
    target="urn:types",
)


class Resolver:
    """A deterministic address resolver; every host defaults to a public IP."""

    def __init__(
        self,
        mapping: dict[str, Any] | None = None,
        *,
        default: Any = GLOBAL_ADDRESS,
    ) -> None:
        self.mapping = dict(mapping or {})
        self.default = default
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> Any:
        self.calls.append((host, port))
        answer = self.mapping.get(host, self.default)
        if isinstance(answer, Exception):
            raise answer
        return [answer] if isinstance(answer, str) else answer


class FakeTransport:
    """A transport that serves prepared responses and records every request."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = dict(responses)
        self.requests: list[str] = []

    def fetch(self, url: str, *, limits: RemoteLimits) -> RemoteResponse:
        self.requests.append(url)
        try:
            response = self.responses[url]
        except KeyError as exc:
            raise RemoteTransportError(f"no fake response for {url}") from exc
        if isinstance(response, Exception):
            raise response
        return response


def response(
    url: str,
    content: str | bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> RemoteResponse:
    body = content.encode("utf-8") if isinstance(content, str) else content
    return RemoteResponse(url, status, headers or {}, body)


def remote_policy(
    transport: FakeTransport, *, resolver: Any = None, **kwargs: Any
) -> RemoteResourcePolicy:
    return RemoteResourcePolicy(
        transport=transport,
        resolver=resolver if resolver is not None else Resolver(),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a remote policy test attempted real network access")

    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


# --- the authorization matrix -------------------------------------------------


def test_remote_root_and_same_origin_dependency_compile() -> None:
    transport = FakeTransport({ROOT: response(ROOT, ROOT_DOC), TYPES: response(TYPES, TYPES_DOC)})
    policy = remote_policy(transport)
    schema = pyxsd.compile(ROOT, resource_policy=policy)
    assert not schema.report.has_errors
    assert transport.requests == [ROOT, TYPES]
    assert schema.parse(io.StringIO("<root>4</root>")).is_valid


def test_same_origin_import_compiles() -> None:
    transport = FakeTransport(
        {ROOT: response(ROOT, IMPORT_ROOT_DOC), TYPES: response(TYPES, IMPORT_TYPES_DOC)}
    )
    policy = remote_policy(transport)
    schema = pyxsd.compile(ROOT, resource_policy=policy)
    assert not schema.report.has_errors
    assert transport.requests == [ROOT, TYPES]
    document = schema.parse(io.StringIO('<root xmlns="urn:root">4</root>'))
    assert document.is_valid


def test_cross_origin_dependency_is_denied_before_a_second_fetch() -> None:
    root = xsd(f'<xs:include schemaLocation="{OTHER}"/><xs:element name="root" type="Number"/>')
    transport = FakeTransport({ROOT: response(ROOT, root), OTHER: response(OTHER, TYPES_DOC)})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="cross-origin"):
        pyxsd.compile(ROOT, resource_policy=policy)
    assert transport.requests == [ROOT]


def test_cross_origin_dependency_is_allowed_by_an_explicit_origin() -> None:
    root = xsd(f'<xs:include schemaLocation="{OTHER}"/><xs:element name="root" type="Number"/>')
    transport = FakeTransport({ROOT: response(ROOT, root), OTHER: response(OTHER, TYPES_DOC)})
    policy = remote_policy(transport, allowed_origins=("https://b.example",))
    schema = pyxsd.compile(ROOT, resource_policy=policy)
    assert not schema.report.has_errors
    assert transport.requests == [ROOT, OTHER]


def test_a_port_difference_is_cross_origin() -> None:
    ported = "https://a.example:8443/types.xsd"
    root = xsd(f'<xs:include schemaLocation="{ported}"/><xs:element name="root" type="Number"/>')
    transport = FakeTransport({ROOT: response(ROOT, root), ported: response(ported, TYPES_DOC)})
    denied = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="cross-origin"):
        pyxsd.compile(ROOT, resource_policy=denied)
    allowed = remote_policy(transport, allowed_origins=("https://a.example:8443",))
    schema = pyxsd.compile(ROOT, resource_policy=allowed)
    assert not schema.report.has_errors


def test_allowed_origins_must_be_https_origins() -> None:
    for origin in (
        "http://b.example",
        "https://",
        "https://b.example/path",
        "https://b.example?q=1",
    ):
        with pytest.raises(ValueError, match="origin"):
            RemoteResourcePolicy(allowed_origins=(origin,))
    for origin in ("https://b.example:99999", "https://b.example:0"):
        with pytest.raises(ValueError, match="invalid port"):
            RemoteResourcePolicy(allowed_origins=(origin,))


@pytest.mark.parametrize(
    "url,message",
    [
        ("https:///root.xsd", "no host"),
        ("https://a.example:99999/root.xsd", "invalid port"),
        ("https://a.example:0/root.xsd", "invalid port"),
    ],
)
def test_malformed_remote_urls_are_denied(url: str, message: str) -> None:
    policy = remote_policy(FakeTransport({}))
    with pytest.raises(ResourcePolicyError, match=message):
        policy.read(url, base=None, purpose="schema")


def test_instance_remote_hints_require_the_hint_flag(tmp_path: Any) -> None:
    instance = tmp_path / "input.xml"
    instance.write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:noNamespaceSchemaLocation="{ROOT}">4</root>',
        encoding="utf-8",
    )
    transport = FakeTransport({ROOT: response(ROOT, SIMPLE_DOC)})
    denied = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="allow_remote_hints"):
        pyxsd.parse(str(instance), resource_policy=denied)
    assert transport.requests == []

    allowed = remote_policy(transport, allow_remote_hints=True)
    document = pyxsd.parse(str(instance), resource_policy=allowed)
    assert document.is_valid
    assert transport.requests == [ROOT]


def test_hint_derived_remote_dependency_from_a_local_schema_needs_both_flags(
    tmp_path: Any,
) -> None:
    (tmp_path / "main.xsd").write_text(SIMPLE_DOC, encoding="utf-8")
    instance = tmp_path / "input.xml"
    instance.write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="urn:extra https://b.example/types.xsd" '
        'xsi:noNamespaceSchemaLocation="main.xsd">4</root>',
        encoding="utf-8",
    )
    transport = FakeTransport({OTHER: response(OTHER, TYPES_DOC)})
    hints_only = remote_policy(transport, allow_remote_hints=True)
    with pytest.raises(ResourcePolicyError, match="allow_local_remote"):
        pyxsd.parse(str(instance), mode=pyxsd.ParseModes.NAMESPACED, resource_policy=hints_only)
    assert transport.requests == []


def test_local_schema_remote_dependency_requires_allow_local_remote(tmp_path: Any) -> None:
    (tmp_path / "main.xsd").write_text(
        xsd(f'<xs:include schemaLocation="{ROOT}"/>'), encoding="utf-8"
    )
    transport = FakeTransport({ROOT: response(ROOT, ROOT_DOC), TYPES: response(TYPES, TYPES_DOC)})
    denied = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="allow_local_remote"):
        pyxsd.compile(str(tmp_path / "main.xsd"), resource_policy=denied)
    assert transport.requests == []

    allowed = remote_policy(transport, allow_local_remote=True)
    schema = pyxsd.compile(str(tmp_path / "main.xsd"), resource_policy=allowed)
    assert not schema.report.has_errors
    assert transport.requests == [ROOT, TYPES]


@pytest.mark.parametrize(
    "pivot",
    [
        "file:///etc/passwd",
        "file:///tmp/types.xsd",
        "C:\\secrets\\types.xsd",
        "c:types.xsd",
    ],
)
def test_remote_to_local_and_file_pivots_are_always_denied(pivot: str) -> None:
    root = xsd(f'<xs:include schemaLocation="{pivot}"/><xs:element name="root" type="xs:string"/>')
    transport = FakeTransport({ROOT: response(ROOT, root)})
    policy = remote_policy(transport, allow_local_remote=True)
    with pytest.raises(ResourcePolicyError, match=r"pivot|not https"):
        pyxsd.compile(ROOT, resource_policy=policy)
    assert transport.requests == [ROOT]


def test_a_remote_base_never_reads_an_absolute_local_path_locally() -> None:
    transport = FakeTransport(
        {"https://a.example/etc/passwd": response("https://a.example/etc/passwd", SIMPLE_DOC)}
    )
    policy = remote_policy(transport)
    data = policy.read("/etc/passwd", base=ROOT, purpose="schema")
    assert data.source_id == "https://a.example/etc/passwd"
    assert transport.requests == ["https://a.example/etc/passwd"]


def test_remote_overlay_is_never_retrieved(tmp_path: Any) -> None:
    transport = FakeTransport({ROOT: response(ROOT, SIMPLE_DOC)})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="overlay"):
        pyxsd.compile(ROOT, overlay="https://evil.example/overlay.py", resource_policy=policy)
    assert transport.requests == []


def test_a_remote_root_cannot_load_a_local_overlay(tmp_path: Any) -> None:
    overlay = tmp_path / "overlay.py"
    overlay.write_text("x = 1\n", encoding="utf-8")
    transport = FakeTransport({ROOT: response(ROOT, SIMPLE_DOC)})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="overlay"):
        pyxsd.compile(ROOT, overlay=overlay, resource_policy=policy)


def test_a_memory_schema_still_loads_an_explicit_local_overlay(tmp_path: Any) -> None:
    overlay = tmp_path / "overlay.py"
    overlay.write_text(
        "from pyxsd.schema_base import SchemaBase\n\nclass Item(SchemaBase):\n    name = 'Item'\n",
        encoding="utf-8",
    )
    policy = remote_policy(FakeTransport({}))
    schema = pyxsd.compile(io.StringIO(SIMPLE_DOC), overlay=overlay, resource_policy=policy)
    assert "Item" in schema.classes


def test_http_is_denied_before_a_resolver_or_fetch() -> None:
    transport = FakeTransport({})
    resolver = Resolver()
    policy = remote_policy(transport, resolver=resolver)
    with pytest.raises(ResourcePolicyError, match="not https"):
        pyxsd.compile("http://a.example/root.xsd", resource_policy=policy)
    with pytest.raises(ResourcePolicyError, match="not https"):
        policy.read("http://a.example/types.xsd", base=ROOT, purpose="schema")
    assert transport.requests == []
    assert resolver.calls == []


def test_remote_instance_documents_are_denied() -> None:
    policy = remote_policy(FakeTransport({}))
    with pytest.raises(ResourcePolicyError, match="instance"):
        policy.read(ROOT, base=None, purpose="instance")


@pytest.mark.parametrize(
    "host,address",
    [
        ("loop.example", "127.0.0.1"),
        ("localhost", "127.0.0.1"),
        ("private.example", "10.0.0.5"),
        ("shared.example", "100.64.0.1"),
        ("link.example", "169.254.1.1"),
        ("v6loop.example", "::1"),
        ("mapped.example", "::ffff:10.0.0.5"),
        ("documentation.example", "192.0.2.1"),
        ("unspecified.example", "0.0.0.0"),
    ],
)
def test_non_public_addresses_are_denied(host: str, address: str) -> None:
    url = f"https://{host}/root.xsd"
    transport = FakeTransport({url: response(url, SIMPLE_DOC)})
    policy = remote_policy(transport, resolver=Resolver({host: [address]}))
    with pytest.raises(ResourcePolicyError, match="non-public"):
        pyxsd.compile(url, resource_policy=policy)
    assert transport.requests == []


def test_trusted_private_addresses_can_be_allowed() -> None:
    url = "https://[::1]/root.xsd"
    transport = FakeTransport({url: response(url, SIMPLE_DOC)})
    resolver = Resolver({"::1": ["::1"]})
    policy = remote_policy(transport, resolver=resolver, allow_private_addresses=True)
    schema = pyxsd.compile(url, resource_policy=policy)
    assert not schema.report.has_errors
    assert resolver.calls == [("::1", 443)]
    data = policy.read(url, base=None, purpose="schema")
    assert data.source_id == url


def test_an_explicit_nondefault_port_is_canonical_and_resolved() -> None:
    url = "https://a.example:8443/root.xsd"
    transport = FakeTransport({url: response(url, SIMPLE_DOC)})
    resolver = Resolver()
    data = remote_policy(transport, resolver=resolver).read(url, base=None, purpose="schema")
    assert data.source_id == url
    assert resolver.calls == [("a.example", 8443)]


def test_resolver_failures_are_policy_errors() -> None:
    policy = remote_policy(FakeTransport({}), resolver=Resolver(default=OSError("no dns")))
    with pytest.raises(ResourcePolicyError, match="could not be resolved"):
        pyxsd.compile(ROOT, resource_policy=policy)

    empty = remote_policy(FakeTransport({}), resolver=Resolver(default=[]))
    with pytest.raises(ResourcePolicyError, match="no addresses"):
        pyxsd.compile(ROOT, resource_policy=empty)

    invalid = remote_policy(FakeTransport({}), resolver=Resolver(default=["not-an-ip"]))
    with pytest.raises(ResourcePolicyError, match="invalid address"):
        pyxsd.compile(ROOT, resource_policy=invalid)


def test_the_default_resolver_uses_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        calls.append((host, port, kwargs))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (GLOBAL_ADDRESS, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    transport = FakeTransport({ROOT: response(ROOT, SIMPLE_DOC)})
    policy = RemoteResourcePolicy(transport=transport)
    schema = pyxsd.compile(ROOT, resource_policy=policy)
    assert not schema.report.has_errors
    assert calls == [("a.example", 443, {"type": socket.SOCK_STREAM})]


# --- redirects ----------------------------------------------------------------


def test_a_same_origin_redirect_is_followed_and_canonicalized() -> None:
    transport = FakeTransport(
        {
            ROOT: response(ROOT, "", status=302, headers={"Location": "real/root.xsd"}),
            FINAL: response(FINAL, SIMPLE_DOC),
        }
    )
    policy = remote_policy(transport)
    data = policy.read(ROOT, base=None, purpose="schema")
    assert data.source_id == FINAL
    assert data.content == SIMPLE_DOC.encode("utf-8")
    assert transport.requests == [ROOT, FINAL]


def test_a_cross_origin_redirect_is_denied_before_the_next_fetch() -> None:
    transport = FakeTransport({ROOT: response(ROOT, "", status=301, headers={"Location": OTHER})})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match=r"redirect.*cross-origin"):
        policy.read(ROOT, base=None, purpose="schema")
    assert transport.requests == [ROOT]


def test_a_cross_origin_redirect_can_be_allowed() -> None:
    transport = FakeTransport(
        {
            ROOT: response(ROOT, "", status=307, headers={"Location": OTHER}),
            OTHER: response(OTHER, SIMPLE_DOC),
        }
    )
    policy = remote_policy(transport, allowed_origins=("https://b.example",))
    data = policy.read(ROOT, base=None, purpose="schema")
    assert data.source_id == OTHER
    assert transport.requests == [ROOT, OTHER]


def test_a_redirect_must_stay_https() -> None:
    transport = FakeTransport(
        {ROOT: response(ROOT, "", status=302, headers={"Location": "http://a.example/x.xsd"})}
    )
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="not https"):
        policy.read(ROOT, base=None, purpose="schema")
    assert transport.requests == [ROOT]


def test_a_redirect_target_is_address_checked_before_the_next_fetch() -> None:
    target = "https://a.example/private.xsd"
    answers = iter([GLOBAL_ADDRESS, "10.0.0.5"])

    def resolver(host: str, port: int) -> list[str]:
        return [next(answers)]

    transport = FakeTransport({ROOT: response(ROOT, "", status=302, headers={"Location": target})})
    policy = remote_policy(transport, resolver=resolver)
    with pytest.raises(ResourcePolicyError, match="non-public"):
        policy.read(ROOT, base=None, purpose="schema")
    assert transport.requests == [ROOT]


def test_a_redirect_without_a_location_is_denied() -> None:
    transport = FakeTransport({ROOT: response(ROOT, "", status=302)})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="Location"):
        policy.read(ROOT, base=None, purpose="schema")


def test_the_redirect_limit_bounds_the_chain() -> None:
    first = "https://a.example/one.xsd"
    second = "https://a.example/two.xsd"
    transport = FakeTransport(
        {
            ROOT: response(ROOT, "", status=302, headers={"Location": first}),
            first: response(first, "", status=302, headers={"Location": second}),
        }
    )
    policy = remote_policy(transport, limits=RemoteLimits(redirects=1))
    with pytest.raises(ResourcePolicyError, match="redirects=1"):
        policy.read(ROOT, base=None, purpose="schema")
    assert transport.requests == [ROOT, first]


def test_a_non_success_response_is_denied() -> None:
    transport = FakeTransport({ROOT: response(ROOT, "gone", status=404)})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match="status 404"):
        policy.read(ROOT, base=None, purpose="schema")


def test_transport_failures_propagate() -> None:
    transport = FakeTransport({ROOT: RemoteTransportError("boom")})
    policy = remote_policy(transport)
    with pytest.raises(RemoteTransportError, match="boom"):
        policy.read(ROOT, base=None, purpose="schema")


# --- closure budgets and deduplication ---------------------------------------


def test_the_per_response_byte_budget_is_enforced() -> None:
    transport = FakeTransport({ROOT: response(ROOT, SIMPLE_DOC)})
    limits = RemoteLimits(response_bytes=len(SIMPLE_DOC) - 1, closure_bytes=len(SIMPLE_DOC))
    policy = remote_policy(transport, limits=limits)
    with pytest.raises(ResourcePolicyError, match="response_bytes"):
        policy.read(ROOT, base=None, purpose="schema")


def test_the_closure_byte_budget_spans_resources() -> None:
    transport = FakeTransport({ROOT: response(ROOT, ROOT_DOC), TYPES: response(TYPES, TYPES_DOC)})
    limit = len(ROOT_DOC) + len(TYPES_DOC) - 1
    limits = RemoteLimits(response_bytes=limit, closure_bytes=limit)
    policy = remote_policy(transport, limits=limits)
    with pytest.raises(ResourcePolicyError, match="closure_bytes"):
        pyxsd.compile(ROOT, resource_policy=policy)
    assert transport.requests == [ROOT, TYPES]


def test_the_resource_count_budget_is_checked_before_a_fetch() -> None:
    transport = FakeTransport({ROOT: response(ROOT, ROOT_DOC), TYPES: response(TYPES, TYPES_DOC)})
    policy = remote_policy(transport, limits=RemoteLimits(resources=1))
    with pytest.raises(ResourcePolicyError, match="resources=1"):
        pyxsd.compile(ROOT, resource_policy=policy)
    assert transport.requests == [ROOT]


def test_the_dependency_depth_budget_is_enforced() -> None:
    transport = FakeTransport(
        {
            ROOT: response(ROOT, xsd('<xs:include schemaLocation="mid.xsd"/>')),
            MID: response(MID, MID_DOC),
            TYPES: response(TYPES, TYPES_DOC),
        }
    )
    policy = remote_policy(transport, limits=RemoteLimits(depth=1))
    with pytest.raises(ResourcePolicyError, match="depth=1"):
        pyxsd.compile(ROOT, resource_policy=policy)
    assert transport.requests == [ROOT, MID]


def test_canonical_urls_deduplicate_repeated_resources() -> None:
    transport = FakeTransport(
        {
            ROOT: response(ROOT, "", status=302, headers={"Location": FINAL}),
            FINAL: response(FINAL, SIMPLE_DOC),
        }
    )
    policy = remote_policy(transport, limits=RemoteLimits(resources=2))
    first = policy.read(ROOT, base=None, purpose="schema")
    second = policy.read(ROOT, base=ROOT, purpose="schema")
    assert first.source_id == second.source_id == FINAL
    assert transport.requests == [ROOT, FINAL, ROOT, FINAL]


def test_repeated_reads_are_accounted_against_the_closure_bytes() -> None:
    transport = FakeTransport({ROOT: response(ROOT, SIMPLE_DOC)})
    limits = RemoteLimits(response_bytes=len(SIMPLE_DOC), closure_bytes=len(SIMPLE_DOC) + 1)
    policy = remote_policy(transport, limits=limits)
    policy.read(ROOT, base=None, purpose="schema")
    with pytest.raises(ResourcePolicyError, match="closure_bytes"):
        policy.read(ROOT, base=ROOT, purpose="schema")


def test_a_dependency_cycle_is_skipped_without_unbounded_retrieval() -> None:
    transport = FakeTransport(
        {
            ROOT: response(ROOT, xsd('<xs:include schemaLocation="mid.xsd"/>')),
            MID: response(MID, xsd('<xs:include schemaLocation="root.xsd"/>')),
        }
    )
    policy = remote_policy(transport)
    schema = pyxsd.compile(ROOT, resource_policy=policy)
    assert not schema.report.has_errors
    assert not schema.report.warnings
    assert transport.requests == [ROOT, MID]


def test_instance_reads_never_enter_the_remote_closure_budget(tmp_path: Any) -> None:
    body = SIMPLE_DOC
    transport = FakeTransport({ROOT: response(ROOT, body)})
    limits = RemoteLimits(response_bytes=len(body), closure_bytes=len(body), resources=1)
    policy = remote_policy(transport, limits=limits)
    schema = pyxsd.compile(ROOT, resource_policy=policy)
    instance = tmp_path / "input.xml"
    instance.write_bytes(b"<root>4</root>")
    document = schema.parse(str(instance))
    assert document.is_valid
    assert transport.requests == [ROOT]


# --- strict parsing and XML limits -------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '<!DOCTYPE xs:schema [<!ENTITY ns "urn:x">]><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>',
        '<!DOCTYPE xs:schema SYSTEM "http://a.example/evil.dtd"><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>',
    ],
)
def test_downloaded_xsd_uses_the_strict_defused_profile(content: str) -> None:
    transport = FakeTransport({ROOT: response(ROOT, content)})
    policy = remote_policy(transport)
    with pytest.raises(ResourcePolicyError, match=r"DOCTYPE|external"):
        pyxsd.compile(ROOT, resource_policy=policy)


def test_remote_xml_limits_bound_depth_and_element_count() -> None:
    content = b"<a><b><c/></b></a>"
    depth_limits = RemoteLimits(
        response_bytes=len(content), closure_bytes=len(content), xml_depth=2
    )
    depth_policy = remote_policy(FakeTransport({}), limits=depth_limits)
    with pytest.raises(ResourcePolicyError, match="max_depth"):
        depth_policy.parse(ResourceData(ROOT, content), NamespaceContext())

    count_limits = RemoteLimits(
        response_bytes=len(content), closure_bytes=len(content), xml_elements=2
    )
    count_policy = remote_policy(FakeTransport({}), limits=count_limits)
    with pytest.raises(ResourcePolicyError, match="max_elements"):
        count_policy.parse(ResourceData(ROOT, content), NamespaceContext())


def test_remote_xml_limits_bound_the_byte_budget() -> None:
    content = b"<a/>" + b" " * 10
    limits = RemoteLimits(response_bytes=len(content) - 1, closure_bytes=len(content))
    policy = remote_policy(FakeTransport({}), limits=limits)
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        policy.parse(ResourceData(ROOT, content), NamespaceContext())


# --- offline defaults and public surface -------------------------------------


def test_no_policy_leaves_remote_roots_denied_without_a_fetch() -> None:
    with pytest.raises(ResourcePolicyError, match="remote-capable resource policy"):
        pyxsd.compile(ROOT)


def test_a_document_authored_remote_dependency_stays_advisory_without_a_policy(
    tmp_path: Any,
) -> None:
    main = tmp_path / "main.xsd"
    main.write_text(
        xsd(f'<xs:include schemaLocation="{ROOT}"/><xs:element name="root" type="xs:string"/>'),
        encoding="utf-8",
    )
    schema = pyxsd.compile(str(main))
    assert not schema.report.has_errors
    assert [issue.code for issue in schema.report.warnings] == ["schema-compose"]


def test_unknown_purposes_are_rejected() -> None:
    transport = FakeTransport({})
    policy = remote_policy(transport)
    with pytest.raises(ValueError, match="purpose"):
        policy.read(ROOT, base=None, purpose="include")
    with pytest.raises(ValueError, match="purpose"):
        read_resource(ROOT, policy, purpose="include")
    with pytest.raises(ValueError, match="purpose"):
        pyxsd.compile(ROOT, resource_policy=policy, _source_purpose="include")
    assert validate_purpose("schema-hint") == "schema-hint"
    assert transport.requests == []


def test_remote_public_types_are_exported() -> None:
    assert pyxsd.RemoteResourcePolicy is RemoteResourcePolicy
    assert pyxsd.RemoteLimits is RemoteLimits
    assert pyxsd.RemoteTransportError is RemoteTransportError
    assert pyxsd.HttpClientTransport is HttpClientTransport
    assert pyxsd.ResourcePolicyError is ResourcePolicyError


# --- reference-resolution pins (Task 3 carry-over) ---------------------------


def test_reference_resolution_on_a_remote_base_uses_rfc_semantics() -> None:
    assert resolve_reference("types.xsd", base=ROOT) == TYPES
    assert resolve_reference("/etc/passwd", base=ROOT) == "https://a.example/etc/passwd"
    assert resolve_reference("//b.example/x.xsd", base=ROOT) == "https://b.example/x.xsd"
    assert resolve_reference("file:///etc/passwd", base=ROOT) == "file:///etc/passwd"
    assert resolve_reference("https://b.example/x.xsd", base=ROOT) == "https://b.example/x.xsd"


def test_windows_drive_relative_paths_are_local() -> None:
    for path in ("c:foo", "C:foo", "c:/foo", "C:\\foo\\bar.xsd", "Z:"):
        assert classify_resource(path).kind == "local"
    assert classify_resource("https://a.example/x.xsd").kind == "remote"
    assert classify_resource("mailto:x@example.com").kind == "remote"
