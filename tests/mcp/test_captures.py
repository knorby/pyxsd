"""Capture manifests and resource-policy limits stay worker-local and bounded."""

import hashlib

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("defusedxml")

from pyxsd.mcp.captures import (
    CaptureFile,
    InputCapture,
    InputPolicyError,
    InputResourcePolicy,
)
from pyxsd.mcp.config import Limits
from pyxsd.namespaces import NamespaceContext
from pyxsd.resource_policy import ResourceData

XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="root" type="xs:string"/></xs:schema>'
)


def capture(tmp_path, files):
    root = tmp_path / "capture"
    root.mkdir(exist_ok=True)
    manifest = []
    for path, content in files.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        manifest.append(CaptureFile(path, hashlib.sha256(content).hexdigest(), len(content)))
    return InputCapture("workspace", 1, root, tuple(manifest))


def test_read_records_dependencies_and_resolves_virtual_paths(tmp_path):
    item = capture(tmp_path, {"schema.xsd": XSD.encode(), "other.xsd": XSD.encode()})
    policy = InputResourcePolicy(item, Limits(schema_closure_files=4))
    first = policy.read("/workspace/schema.xsd", base=None, purpose="schema")
    assert first.content == XSD.encode()
    policy.read("other.xsd", base="/workspace/schema.xsd", purpose="schema")
    policy.read("other.xsd", base="/workspace/schema.xsd", purpose="schema")
    assert [(dep.path, dep.digest is not None) for dep in policy.dependencies] == [
        ("other.xsd", True),
        ("schema.xsd", True),
    ]
    with pytest.raises(FileNotFoundError):
        policy.read("/workspace/missing.xsd", base=None, purpose="schema")
    assert any(dep.path == "missing.xsd" for dep in policy.dependencies)
    with pytest.raises(FileNotFoundError):
        policy.read("/workspace/instance.xml", base=None, purpose="instance")
    assert [dep.path for dep in policy.dependencies].count("instance.xml") == 0
    assert policy.new_report().error_count == 0


@pytest.mark.parametrize(
    "source,base,error",
    [
        ("/etc/passwd", None, "invalid_path"),
        ("C:escape.xsd", None, "invalid_path"),
        ("bad\\path.xsd", None, "invalid_path"),
        ("child.xsd", "not-a-workspace-base", "invalid_path"),
        ("../escape.xsd", "/workspace/schema.xsd", "invalid_path"),
    ],
)
def test_unadmitted_resource_names_are_rejected(tmp_path, source, base, error):
    policy = InputResourcePolicy(capture(tmp_path, {"schema.xsd": XSD.encode()}), Limits())
    with pytest.raises(InputPolicyError, match=error):
        policy.read(source, base=base, purpose="schema")


def test_empty_normalized_resource_path_is_rejected(tmp_path):
    policy = InputResourcePolicy(capture(tmp_path, {"schema.xsd": XSD.encode()}), Limits())
    with pytest.raises(InputPolicyError, match="invalid_path"):
        policy.read("/workspace/bad\x01name", base=None, purpose="schema")


def test_manifest_construction_and_capture_bytes_are_verified(tmp_path):
    with pytest.raises(InputPolicyError, match="path_collision"):
        InputResourcePolicy(
            InputCapture(
                "w",
                1,
                tmp_path,
                (CaptureFile("a", "0" * 64, 1), CaptureFile("a", "0" * 64, 1)),
            ),
            Limits(),
        )
    with pytest.raises(InputPolicyError, match="invalid_path"):
        InputResourcePolicy(
            InputCapture("w", 1, tmp_path, (CaptureFile("a/../b", "0" * 64, 1),)),
            Limits(),
        )
    item = capture(tmp_path, {"schema.xsd": XSD.encode()})
    policy = InputResourcePolicy(item, Limits())
    (item.root / "schema.xsd").write_bytes(b"changed")
    with pytest.raises(InputPolicyError, match="source_changed"):
        policy.read("schema.xsd", base=None, purpose="schema")
    (item.root / "schema.xsd").unlink()
    with pytest.raises(InputPolicyError, match="source_changed"):
        policy.read("schema.xsd", base=None, purpose="schema")
    (item.root / "schema.xsd").symlink_to(tmp_path / "outside")
    with pytest.raises(InputPolicyError, match="unsafe_path"):
        policy.read("schema.xsd", base=None, purpose="schema")


def test_schema_and_instance_budgets_fail_closed(tmp_path):
    item = capture(
        tmp_path,
        {"schema.xsd": XSD.encode(), "other.xsd": XSD.encode(), "doc.xml": b"<root/>"},
    )
    with pytest.raises(InputPolicyError, match="schema_limit"):
        InputResourcePolicy(item, Limits(schema_file_bytes=4)).read(
            "schema.xsd", base=None, purpose="schema"
        )
    with pytest.raises(InputPolicyError, match="xml_limit"):
        InputResourcePolicy(item, Limits(xml_file_bytes=4)).read(
            "doc.xml", base=None, purpose="instance"
        )
    one_file = InputResourcePolicy(item, Limits(schema_closure_files=1))
    one_file.read("schema.xsd", base=None, purpose="schema")
    with pytest.raises(InputPolicyError, match="schema_limit"):
        one_file.read("other.xsd", base="/workspace/schema.xsd", purpose="schema")
    exact_bytes = InputResourcePolicy(item, Limits(schema_closure_bytes=len(XSD)))
    exact_bytes.read("schema.xsd", base=None, purpose="schema")
    with pytest.raises(InputPolicyError, match="schema_limit"):
        exact_bytes.read("other.xsd", base="/workspace/schema.xsd", purpose="schema")


def test_xml_parser_enforces_depth_elements_and_defused_constructs(tmp_path):
    item = capture(tmp_path, {"schema.xsd": XSD.encode()})
    root = InputResourcePolicy(item, Limits()).parse(
        ResourceData("/workspace/a.xml", b'<root xmlns:p="urn:test"><p:child/></root>'),
        NamespaceContext(),
    )
    assert root.tag == "root"
    with pytest.raises(InputPolicyError, match="xml_limit"):
        InputResourcePolicy(item, Limits(xml_file_bytes=4)).parse(
            ResourceData("/workspace/a.xml", b"<root/>"), NamespaceContext()
        )
    with pytest.raises(InputPolicyError, match="xml_limit"):
        InputResourcePolicy(item, Limits(xml_depth=1)).parse(
            ResourceData("/workspace/a.xml", b"<root><child/></root>"), NamespaceContext()
        )
    with pytest.raises(InputPolicyError, match="xml_limit"):
        InputResourcePolicy(item, Limits(xml_elements=1)).parse(
            ResourceData("/workspace/a.xml", b"<root><child/></root>"), NamespaceContext()
        )
    with pytest.raises(InputPolicyError, match="dangerous_xml"):
        InputResourcePolicy(item, Limits()).parse(
            ResourceData(
                "/workspace/a.xml",
                b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
            ),
            NamespaceContext(),
        )


def test_rebind_rejects_invalid_capture_paths(tmp_path):
    from pyxsd.mcp.captures import CaptureFile, InputPolicyError

    policy = InputResourcePolicy(InputCapture("w", 1, tmp_path, ()), Limits())
    with pytest.raises(InputPolicyError, match="invalid_path"):
        policy.rebind(InputCapture("w", 2, tmp_path, (CaptureFile("../x", "0" * 64, 1),)))
