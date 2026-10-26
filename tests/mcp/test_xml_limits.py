"""The snapshot resource policy parses only in clean workers, not services."""

import hashlib
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest

from pyxsd import Schema
from pyxsd.mcp.config import Limits
from pyxsd.namespaces import NamespaceContext
from pyxsd.resource_policy import ResourceData, ResourcePolicyError


def policy_for(root, contents, **limits):
    from pyxsd.mcp.snapshots import Snapshot, SnapshotFile, WorkspaceResourcePolicy

    records = []
    for path, content in contents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append(SnapshotFile(path, hashlib.sha256(content).hexdigest(), len(content)))
    return WorkspaceResourcePolicy(
        Snapshot("workspace", "revision", root, tuple(records)), Limits(**limits)
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize(
    "declaration",
    [
        "<!DOCTYPE root>",
        '<!DOCTYPE root [<!ENTITY x "expanded">]>',
        '<!DOCTYPE root SYSTEM "file:///must-not-read">',
    ],
)
def test_dtd_and_entities_are_denied_even_when_utf16(tmp_path, encoding, declaration):
    policy = policy_for(tmp_path, {})
    xml = f'<?xml version="1.0" encoding="{encoding}"?>{declaration}<root/>'
    with pytest.raises(ResourcePolicyError, match="dangerous_xml"):
        policy.parse(ResourceData("<memory>", xml.encode(encoding)), NamespaceContext())


def test_original_encoding_and_nested_namespace_scopes_are_preserved(tmp_path):
    content = b'<?xml version="1.0" encoding="iso-8859-1"?><r xmlns:p="urn:one"><c xmlns:p="urn:two">caf\xe9</c></r>'
    policy = policy_for(tmp_path, {"input.xml": content})
    data = policy.read("input.xml", base=None, purpose="instance")
    context = NamespaceContext()
    root = policy.parse(data, context)
    assert data.content == content
    assert root[0].text == "caf\u00e9"
    assert context.resolve(root, "p:T") == "{urn:one}T"
    assert context.resolve(root[0], "p:T") == "{urn:two}T"


@pytest.mark.parametrize(
    "limits,xml",
    [
        ({"xml_depth": 1}, b"<r><c/></r>"),
        ({"xml_elements": 2}, b"<r><c/><c/></r>"),
        ({"xml_file_bytes": 3}, b"<r/>"),
    ],
)
def test_xml_budgets_fail_closed(tmp_path, limits, xml):
    policy = policy_for(tmp_path, {}, **limits)
    with pytest.raises(ResourcePolicyError, match="xml_limit"):
        policy.parse(ResourceData("<memory>", xml), NamespaceContext())


def test_exact_xml_budgets_and_malformed_input(tmp_path):
    policy = policy_for(tmp_path, {}, xml_depth=2, xml_elements=2, xml_file_bytes=11)
    assert policy.parse(ResourceData("<memory>", b"<r><c/></r>"), NamespaceContext()).tag == "r"
    with pytest.raises(ET.ParseError):
        policy.parse(ResourceData("<memory>", b"<r>"), NamespaceContext())


@pytest.mark.parametrize(
    "source,base",
    [
        ("../../outside", "/workspace/schema/root.xsd"),
        ("/etc/passwd", None),
        ("https://example/schema", None),
        ("..\\secret", None),
        ("child", "/unadmitted/root.xsd"),
    ],
)
def test_dependency_paths_cannot_escape_the_snapshot(tmp_path, source, base):
    policy = policy_for(tmp_path, {})
    with pytest.raises(ResourcePolicyError, match="invalid_path"):
        policy.read(source, base=base, purpose="include")


def test_nested_composition_reads_and_missing_attempts_are_scoped(tmp_path):
    prefix = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    contents = {
        "schemas/root.xsd": (
            prefix
            + '<xs:include schemaLocation="../shared/base.xsd"/><xs:element name="root" type="T"/></xs:schema>'
        ).encode(),
        "shared/base.xsd": (
            prefix
            + '<xs:simpleType name="T"><xs:restriction base="xs:int"/></xs:simpleType></xs:schema>'
        ).encode(),
    }
    policy = policy_for(tmp_path, contents)
    schema = Schema.compile("schemas/root.xsd", resource_policy=policy)
    assert not schema.report.has_errors
    document = schema.parse(__import__("io").BytesIO(b"<root>5</root>"))
    assert int(document.root) == 5
    assert {item.path for item in policy.dependencies} == set(contents)
    with pytest.raises(FileNotFoundError):
        policy.read("missing.xsd", base="/workspace/shared/base.xsd", purpose="include")
    missing = next(item for item in policy.dependencies if item.path == "shared/missing.xsd")
    assert missing.digest is None and missing.size == 0
    before = policy.dependencies
    with pytest.raises(FileNotFoundError):
        policy.read("missing.xsd", base="/workspace/shared/base.xsd", purpose="include")
    assert policy.dependencies == before


@pytest.mark.parametrize(
    "limits",
    [
        {"schema_closure_files": 1},
        {"schema_closure_bytes": 3},
        {"schema_file_bytes": 1},
    ],
)
def test_schema_closure_and_file_budgets_include_root_and_failed_attempts(tmp_path, limits):
    policy = policy_for(tmp_path, {"one.xsd": b"<r/>", "two.xsd": b"<r/>"}, **limits)
    with pytest.raises(ResourcePolicyError, match="schema_limit"):
        policy.read("one.xsd", base=None, purpose="schema")
        policy.read("two.xsd", base=None, purpose="include")


def test_repeated_reads_do_not_spend_closure_bytes_twice(tmp_path):
    policy = policy_for(
        tmp_path, {"one.xsd": b"<r/>"}, schema_closure_files=1, schema_closure_bytes=4
    )
    policy.read("one.xsd", base=None, purpose="schema")
    policy.read("one.xsd", base=None, purpose="schema")
    assert len(policy.dependencies) == 1
    with pytest.raises(ResourcePolicyError, match="schema_limit"):
        policy.read("missing.xsd", base=None, purpose="include")


def test_missing_attempts_are_bounded_and_instances_do_not_accumulate_in_schema_cache(tmp_path):
    policy = policy_for(tmp_path, {"input.xml": b"<r/>"}, schema_closure_files=1)
    policy.read("input.xml", base=None, purpose="instance")
    assert policy.dependencies == ()
    with pytest.raises(FileNotFoundError):
        policy.read("missing", base=None, purpose="include")
    with pytest.raises(ResourcePolicyError, match="schema_limit"):
        policy.read("another", base=None, purpose="include")


def test_changed_or_unadmitted_snapshot_files_fail_closed(tmp_path):
    policy = policy_for(tmp_path, {"input": b"data"})
    (tmp_path / "input").write_bytes(b"fake")
    with pytest.raises(ResourcePolicyError, match="source_changed"):
        policy.read("input", base=None, purpose="instance")
    (tmp_path / "input").unlink()
    (tmp_path / "target").write_bytes(b"data")
    (tmp_path / "input").symlink_to(tmp_path / "target")
    with pytest.raises(ResourcePolicyError, match="unsafe_path"):
        policy.read("input", base=None, purpose="instance")


def test_manifested_file_disappearing_is_not_an_advisory_missing_import(tmp_path):
    policy = policy_for(tmp_path, {"input.xsd": b"<r/>"})
    (tmp_path / "input.xsd").unlink()
    with pytest.raises(ResourcePolicyError, match="source_changed"):
        policy.read("input.xsd", base=None, purpose="include")


def test_duplicate_snapshot_paths_are_rejected(tmp_path):
    from pyxsd.mcp.snapshots import WorkspaceResourcePolicy

    policy = policy_for(tmp_path, {"one": b"data"})
    with pytest.raises(ResourcePolicyError, match="path_collision"):
        WorkspaceResourcePolicy(replace(policy.snapshot, files=policy.snapshot.files * 2), Limits())


def test_canonical_source_names_anonymous_bases_and_missing_instances(tmp_path):
    policy = policy_for(tmp_path, {"input": b"<r/>"})
    data = policy.read("/workspace/input", base=None, purpose="instance")
    assert data.source_id == "/workspace/input"
    assert policy.read("input", base="<memory>", purpose="instance") == data
    with pytest.raises(FileNotFoundError):
        policy.read("missing", base=None, purpose="instance")
    assert policy.dependencies == ()
    with pytest.raises(ResourcePolicyError, match="invalid_path"):
        policy.read("bad\x00name", base=None, purpose="instance")


def test_all_three_defusedxml_flags_are_set(tmp_path, monkeypatch):
    from defusedxml import ElementTree

    original = ElementTree.iterparse
    calls = []

    def recording_parser(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(ElementTree, "iterparse", recording_parser)
    policy = policy_for(tmp_path, {})
    policy.parse(ResourceData("<memory>", b"<r/>"), NamespaceContext())
    assert calls[0]["forbid_dtd"] is True
    assert calls[0]["forbid_entities"] is True
    assert calls[0]["forbid_external"] is True
