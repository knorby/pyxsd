"""URI-aware resource references resolve without any retrieval.

Canonical source IDs stay strings. Local and virtual policy paths resolve
with filesystem semantics, remote bases with URI semantics, and an absolute
reference is never reinterpreted as a path of another kind. Nothing in this
module opens, fetches, or otherwise touches a resource.
"""

import io
import socket
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import pytest

import pyxsd
from pyxsd.namespaces import XSI_NS, parse_with_namespaces
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.resource_refs import (
    ResourceRef,
    classify_resource,
    resolve_reference,
    resource_base,
)
from pyxsd.schema_hints import absolute_schema_location_pairs, resolve_schema_hint

XS = "http://www.w3.org/2001/XMLSchema"
MAIN = "https://schemas.example/a/main.xsd"
TYPES = "https://schemas.example/a/types.xsd"


def _refuse_network(monkeypatch):
    """Fail loudly if an offline reference ever creates a socket."""

    def deny(*args, **kwargs):
        raise AssertionError("an offline reference must not create a socket")

    monkeypatch.setattr(socket, "socket", deny)


def _forbid_url_open(monkeypatch):
    """Record opened names and fail on a URL passed to ``open()``."""
    opened = []
    real_open = open

    def guarded(file, *args, **kwargs):
        name = str(file)
        opened.append(name)
        if "://" in name:
            raise AssertionError(f"an offline reference must not open {name!r}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)
    return opened


@pytest.mark.parametrize(
    "source,kind",
    [
        ("/workspace/schemas/main.xsd", "local"),
        ("relative/main.xsd", "local"),
        ("<memory>", "memory"),
        (MAIN, "remote"),
        ("http://schemas.example/a/main.xsd", "remote"),
        ("file:///etc/passwd", "remote"),
        ("C:\\schemas\\main.xsd", "local"),
        ("C:/schemas/main.xsd", "local"),
    ],
)
def test_classify_resource_separates_local_remote_and_memory(source, kind):
    assert classify_resource(source) == ResourceRef(kind, source)


def test_classify_resource_accepts_path_objects(tmp_path):
    path = tmp_path / "main.xsd"
    assert classify_resource(path) == ResourceRef("local", str(path))


def test_absolute_local_reference_is_canonical(tmp_path):
    unresolved = str(tmp_path / "schemas" / ".." / "main.xsd")
    assert resolve_reference(unresolved, base=None) == str(tmp_path / "main.xsd")


def test_relative_reference_uses_filesystem_semantics(tmp_path):
    base = str(tmp_path / "schemas" / "main.xsd")
    assert resolve_reference("parts/types.xsd", base=base) == str(
        tmp_path / "schemas/parts/types.xsd"
    )
    assert resolve_reference("../shared.xsd", base=base) == str(tmp_path / "shared.xsd")


def test_virtual_workspace_paths_keep_their_shape():
    base = "/workspace/schemas/main.xsd"
    assert resolve_reference("parts/types.xsd", base=base) == "/workspace/schemas/parts/types.xsd"
    assert resolve_reference("../shared.xsd", base=base) == "/workspace/shared.xsd"


def test_anonymous_base_resolves_relative_references_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nested").mkdir()
    assert resolve_reference("types.xsd", base=None) == str(tmp_path / "types.xsd")
    assert resolve_reference("nested/types.xsd", base=None) == str(tmp_path / "nested/types.xsd")


def test_memory_base_rejects_relative_references():
    with pytest.raises(ResourcePolicyError, match="named source base"):
        resolve_reference("types.xsd", base="<memory>")


def test_memory_base_keeps_absolute_references():
    assert resolve_reference("/workspace/types.xsd", base="<memory>") == "/workspace/types.xsd"
    assert resolve_reference(MAIN, base="<memory>") == MAIN


def test_remote_base_uses_uri_resolution():
    assert resolve_reference("types.xsd", base=MAIN) == TYPES
    assert resolve_reference("../shared.xsd", base=MAIN) == "https://schemas.example/shared.xsd"
    assert (
        resolve_reference("https://other.example/x.xsd", base=MAIN) == "https://other.example/x.xsd"
    )


def test_remote_references_preserve_query_and_fragment():
    assert resolve_reference("types.xsd?v=2#part", base=MAIN) == f"{TYPES}?v=2#part"
    absolute = "https://other.example/types.xsd?v=2#part"
    assert resolve_reference(absolute, base=MAIN) == absolute


def test_remote_reference_is_not_joined_onto_a_local_base(tmp_path):
    base = str(tmp_path / "schemas" / "main.xsd")
    assert resolve_reference(MAIN, base=base) == MAIN


def test_absolute_local_reference_on_a_local_base_is_canonical(tmp_path):
    base = str(tmp_path / "schemas" / "main.xsd")
    absolute = str(tmp_path / "shared" / ".." / "shared" / "types.xsd")
    assert resolve_reference(absolute, base=base) == str(tmp_path / "shared/types.xsd")


def test_resource_base_canonicalizes_document_ids(tmp_path):
    assert resource_base(None) is None
    assert resource_base("<memory>") == "<memory>"
    assert resource_base(MAIN) == MAIN
    assert resource_base(tmp_path / "schemas" / ".." / "main.xsd") == str(tmp_path / "main.xsd")


def test_remote_root_without_a_policy_is_a_policy_error(monkeypatch):
    _refuse_network(monkeypatch)
    opened = _forbid_url_open(monkeypatch)
    with pytest.raises(ResourcePolicyError, match="remote-capable resource policy"):
        pyxsd.compile(MAIN)
    assert MAIN not in opened


@pytest.mark.parametrize("directive", ["include", "import"])
def test_remote_document_dependency_is_an_advisory_hint_offline(tmp_path, monkeypatch, directive):
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:{directive} schemaLocation="{MAIN}"/>'
        '<xs:element name="root" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    _refuse_network(monkeypatch)
    opened = _forbid_url_open(monkeypatch)
    schema = pyxsd.compile(str(tmp_path / "main.xsd"))
    # A schema document's own schemaLocation is advisory (XSD §4.2), so an
    # unavailable remote document keeps its existing missing-resource
    # warning -- it is never opened here.
    assert not schema.report.has_errors
    assert [issue.code for issue in schema.report.warnings] == [
        "import-unresolved" if directive == "import" else "schema-compose"
    ]
    assert "remote-capable resource policy" in schema.report.warnings[0].message
    assert MAIN not in opened


def test_remote_namespace_schema_without_a_policy_is_a_policy_error(tmp_path, monkeypatch):
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" xmlns:e="urn:e"><xs:import namespace="urn:e"/>'
        '<xs:element name="root" type="e:Number"/></xs:schema>',
        encoding="utf-8",
    )
    _refuse_network(monkeypatch)
    opened = _forbid_url_open(monkeypatch)
    with pytest.raises(ResourcePolicyError, match="remote-capable resource policy"):
        pyxsd.compile(str(tmp_path / "main.xsd"), namespace_schemas={"urn:e": MAIN})
    assert MAIN not in opened


def test_remote_instance_hint_without_a_policy_is_a_policy_error(tmp_path, monkeypatch):
    instance = tmp_path / "input.xml"
    instance.write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:noNamespaceSchemaLocation="{MAIN}">4</root>',
        encoding="utf-8",
    )
    _refuse_network(monkeypatch)
    opened = _forbid_url_open(monkeypatch)
    with pytest.raises(ResourcePolicyError, match="remote-capable resource policy"):
        pyxsd.parse(str(instance))
    assert MAIN not in opened


def test_file_uri_is_denied_rather_than_opened(monkeypatch):
    _refuse_network(monkeypatch)
    opened = _forbid_url_open(monkeypatch)
    with pytest.raises(ResourcePolicyError, match="remote-capable resource policy"):
        pyxsd.compile("file:///etc/passwd")
    assert not opened


class UriPolicy:
    """A trusted policy serving a fixed document set over canonical URIs."""

    def __init__(self, documents):
        self.documents = documents
        self.reads = []
        self.parsed = []

    def read(self, source, *, base, purpose):
        reference = urljoin(base, source) if base is not None else source
        self.reads.append((source, base, purpose, reference))
        return ResourceData(reference, self.documents[reference])

    def parse(self, data, context):
        root = parse_with_namespaces(io.BytesIO(data.content), context)
        self.parsed.append(data.source_id)
        return root


def test_trusted_policy_serves_a_remote_main_and_same_origin_dependency(monkeypatch):
    _refuse_network(monkeypatch)
    policy = UriPolicy(
        {
            MAIN: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="types.xsd"/>'
                '<xs:element name="root" type="Number"/></xs:schema>'
            ).encode(),
            TYPES: (
                f'<xs:schema xmlns:xs="{XS}"><xs:simpleType name="Number">'
                '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>'
            ).encode(),
        }
    )
    schema = pyxsd.compile(MAIN, resource_policy=policy)
    assert not schema.report.has_errors
    assert policy.reads == [
        (MAIN, None, "schema", MAIN),
        ("types.xsd", MAIN, "schema", TYPES),
    ]
    assert policy.parsed == [MAIN, TYPES]
    assert schema.parse(io.StringIO("<root>4</root>")).is_valid


def test_remote_include_of_its_own_main_document_is_deduplicated(monkeypatch):
    _refuse_network(monkeypatch)
    policy = UriPolicy(
        {
            MAIN: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="types.xsd"/>'
                '<xs:element name="root" type="Number"/></xs:schema>'
            ).encode(),
            TYPES: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="main.xsd"/>'
                '<xs:simpleType name="Number"><xs:restriction base="xs:int"/>'
                "</xs:simpleType></xs:schema>"
            ).encode(),
        }
    )
    schema = pyxsd.compile(MAIN, resource_policy=policy)
    assert not schema.report.has_errors
    assert not schema.report.warnings
    assert len(policy.reads) == 2


def test_remote_dependency_cycle_is_detected_with_uri_keys(monkeypatch):
    _refuse_network(monkeypatch)
    a = "https://schemas.example/a/a.xsd"
    b = "https://schemas.example/a/b.xsd"
    policy = UriPolicy(
        {
            MAIN: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="a.xsd"/>'
                '<xs:element name="root" type="Number"/></xs:schema>'
            ).encode(),
            a: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="b.xsd"/>'
                '<xs:simpleType name="Number"><xs:restriction base="xs:int"/>'
                "</xs:simpleType></xs:schema>"
            ).encode(),
            b: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="a.xsd"/></xs:schema>'
            ).encode(),
        }
    )
    schema = pyxsd.compile(MAIN, resource_policy=policy)
    assert not schema.report.has_errors
    assert [issue.code for issue in schema.report.warnings] == ["compose-cycle"]
    assert len(policy.reads) == 3


def test_remote_dependency_preserves_query_in_its_read(monkeypatch):
    _refuse_network(monkeypatch)
    policy = UriPolicy(
        {
            MAIN: (
                f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="types.xsd?v=2#part"/>'
                '<xs:element name="root" type="Number"/></xs:schema>'
            ).encode(),
            f"{TYPES}?v=2#part": (
                f'<xs:schema xmlns:xs="{XS}"><xs:simpleType name="Number">'
                '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>'
            ).encode(),
        }
    )
    schema = pyxsd.compile(MAIN, resource_policy=policy)
    assert not schema.report.has_errors
    assert policy.reads[-1] == ("types.xsd?v=2#part", MAIN, "schema", f"{TYPES}?v=2#part")


def _instance_with_hint(location):
    return ET.fromstring(f'<root xmlns:xsi="{XSI_NS}" xsi:noNamespaceSchemaLocation="{location}"/>')


def test_instance_hint_references_stay_local_absolute_or_a_uri(tmp_path):
    absolute = str(tmp_path / "main.xsd")
    assert resolve_schema_hint(_instance_with_hint(absolute), tmp_path) == (None, absolute)
    assert absolute_schema_location_pairs(_instance_with_hint(absolute), tmp_path) == [
        (None, absolute)
    ]
    assert absolute_schema_location_pairs(_instance_with_hint("main.xsd"), tmp_path) == [
        (None, absolute)
    ]
    assert resolve_schema_hint(_instance_with_hint(MAIN), tmp_path) == (None, MAIN)
    assert absolute_schema_location_pairs(_instance_with_hint(MAIN), tmp_path) == [(None, MAIN)]
    assert absolute_schema_location_pairs(_instance_with_hint("main.xsd"), None) == [
        (None, "main.xsd")
    ]
