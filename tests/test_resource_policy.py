"""Every opt-in resource access and parse goes through the caller's policy."""

import io
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import pyxsd
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

XS = "http://www.w3.org/2001/XMLSchema"
SIMPLE = f'<xs:schema xmlns:xs="{XS}"><xs:element name="root" type="xs:int"/></xs:schema>'


class RecordingPolicy:
    def __init__(self, root):
        self.root = root
        self.reads = []
        self.parsed = []
        self.denied = set()

    def read(self, source, *, base, purpose):
        from pyxsd.resource_policy import ResourceData, ResourcePolicyError

        path = Path(source)
        if not path.is_absolute():
            path = (Path(base).parent if base is not None else self.root) / path
        path = path.resolve()
        self.reads.append((path, base, purpose))
        if path.name in self.denied or not path.is_relative_to(self.root):
            raise ResourcePolicyError("resource denied")
        return ResourceData(str(path), path.read_bytes())

    def parse(self, data, context):
        root = parse_with_namespaces(io.BytesIO(data.content), context)
        self.parsed.append((data.source_id, data.content, root))
        return root


def test_optional_report_factory_bounds_schema_instance_and_merged_reports(tmp_path):
    from pyxsd.validation import ValidationReport

    class BoundedPolicy(RecordingPolicy):
        def __init__(self, root):
            super().__init__(root)
            self.reports = []

        def new_report(self):
            report = ValidationReport(max_issues=0, max_message_bytes=16)
            self.reports.append(report)
            return report

    source = (
        f'<xs:schema xmlns:xs="{XS}"><xs:element name="r" type="xs:string"/>'
        '<xs:element name="r" type="xs:int"/></xs:schema>'
    )
    policy = BoundedPolicy(tmp_path)
    schema = pyxsd.compile(io.StringIO(source), resource_policy=policy)
    document = schema.parse(io.StringIO("<r>bad</r>"))
    assert [report.error_count for report in policy.reports] == [1, 1, 2]
    assert all(len(report) == 0 for report in policy.reports)
    assert schema.report is policy.reports[0]
    assert document.report is policy.reports[2]

    unlimited = RecordingPolicy(tmp_path)
    schema = pyxsd.compile(io.StringIO(source), resource_policy=unlimited)
    document = schema.parse(io.StringIO("<r>bad</r>"))
    assert len(schema.report) == 1
    assert len(document.report) == 2


@pytest.mark.parametrize("directive", ["include", "import", "redefine", "override"])
def test_composition_reads_and_parses_have_the_including_source_base(tmp_path, directive):
    (tmp_path / "parts").mkdir()
    imported = directive == "import"
    namespace = ' namespace="urn:types"' if imported else ""
    target = ' targetNamespace="urn:types"' if imported else ""
    value_type = "n:Number" if imported else "Number"
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" xmlns:n="urn:types"><xs:{directive}{namespace} schemaLocation="parts/types.xsd"/>'
        f'<xs:element name="root" type="{value_type}"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "parts/types.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"{target}><xs:include schemaLocation="value.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "parts/value.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:simpleType name="Number">'
        '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)
    schema = pyxsd.compile("main.xsd", resource_policy=policy)
    assert not schema.report.has_errors
    assert policy.reads == [
        (tmp_path / "main.xsd", None, "schema"),
        (tmp_path / "parts/types.xsd", str(tmp_path / "main.xsd"), "schema"),
        (tmp_path / "parts/value.xsd", str(tmp_path / "parts/types.xsd"), "schema"),
    ]
    assert len(policy.parsed) == 3
    assert schema.parse(io.StringIO("<root>4</root>")).is_valid
    assert schema.resource_policy is policy


@pytest.mark.parametrize("directive", ["include", "import", "redefine", "override"])
def test_policy_denials_are_not_missing_import_warnings(tmp_path, directive):
    from pyxsd.resource_policy import ResourcePolicyError

    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:{directive} schemaLocation="secret.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)
    policy.denied.add("secret.xsd")
    with pytest.raises(ResourcePolicyError, match="denied"):
        pyxsd.compile(tmp_path / "main.xsd", resource_policy=policy)
    assert policy.reads[-1][0].name == "secret.xsd"


def test_instance_hints_and_namespace_mappings_are_policy_reads(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "instances").mkdir()
    (tmp_path / "schemas/main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" xmlns:n="urn:extra"><xs:import namespace="urn:extra"/>'
        '<xs:element name="root" type="n:Number"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "schemas/extra.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:extra"><xs:simpleType name="Number">'
        '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "instances/input.xml").write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="../schemas/main.xsd">4</root>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)
    document = pyxsd.parse(
        "instances/input.xml",
        mode=pyxsd.ParseModes.NAMESPACED,
        namespace_schemas={"urn:extra": "extra.xsd"},
        resource_policy=policy,
    )
    assert document.is_valid
    assert [path for path, _, _ in policy.reads] == [
        tmp_path / "instances/input.xml",
        tmp_path / "schemas/main.xsd",
        tmp_path / "schemas/extra.xsd",
    ]
    assert policy.parsed[0][0] == str(tmp_path / "instances/input.xml")
    assert document.source == "instances/input.xml"


def test_instance_selected_main_schema_and_hints_are_schema_hint_reads(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "instances").mkdir()
    (tmp_path / "schemas/main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" xmlns:n="urn:extra"><xs:import namespace="urn:extra"/>'
        '<xs:element name="root" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "schemas/extra.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:extra"><xs:simpleType name="Number">'
        '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "schemas/hint.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:hint">'
        '<xs:include schemaLocation="hint-types.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "schemas/hint-types.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:simpleType name="Hinted">'
        '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "instances/input.xml").write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="../schemas/main.xsd" '
        'xsi:schemaLocation="urn:hint ../schemas/hint.xsd">text</root>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)
    document = pyxsd.parse(
        "instances/input.xml",
        mode=pyxsd.ParseModes.NAMESPACED,
        namespace_schemas={"urn:extra": "extra.xsd"},
        resource_policy=policy,
    )
    assert not document.report.has_errors
    assert [(path.name, base, purpose) for path, base, purpose in policy.reads] == [
        ("input.xml", None, "instance"),
        ("main.xsd", None, "schema-hint"),
        ("extra.xsd", str(tmp_path / "schemas/main.xsd"), "schema"),
        ("hint.xsd", str(tmp_path / "schemas/main.xsd"), "schema-hint"),
        ("hint-types.xsd", str(tmp_path / "schemas/hint.xsd"), "schema"),
    ]


def test_explicit_schema_with_instance_hints_uses_schema_and_hint_purposes(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "instances").mkdir()
    (tmp_path / "schemas/main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:element name="root" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "schemas/hint.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:hint">'
        '<xs:include schemaLocation="hint-types.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "schemas/hint-types.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:simpleType name="Hinted">'
        '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "instances/input.xml").write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="../schemas/main.xsd" '
        'xsi:schemaLocation="urn:hint ../schemas/hint.xsd">text</root>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)
    document = pyxsd.parse(
        "instances/input.xml",
        xsd="schemas/main.xsd",
        mode=pyxsd.ParseModes.NAMESPACED,
        resource_policy=policy,
    )
    assert not document.report.has_errors
    assert [(path.name, purpose) for path, _, purpose in policy.reads] == [
        ("input.xml", "instance"),
        ("main.xsd", "schema"),
        ("hint.xsd", "schema-hint"),
        ("hint-types.xsd", "schema"),
    ]


def test_root_remote_denial_names_the_real_purpose(tmp_path):
    from pyxsd.resource_policy import ResourcePolicyError

    remote = "https://schemas.example/main.xsd"
    with pytest.raises(ResourcePolicyError, match="the schema reference"):
        pyxsd.compile(remote)
    (tmp_path / "input.xml").write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:noNamespaceSchemaLocation="{remote}">4</root>',
        encoding="utf-8",
    )
    with pytest.raises(ResourcePolicyError, match="the schema-hint reference"):
        pyxsd.parse(str(tmp_path / "input.xml"))


def test_streams_and_revalidation_are_policy_parsed_without_extra_reads(tmp_path):
    policy = RecordingPolicy(tmp_path)
    schema = pyxsd.compile(io.StringIO(SIMPLE), resource_policy=policy)
    document = schema.parse(io.StringIO("<root>4</root>"))
    assert document.revalidate().is_valid
    assert policy.reads == []
    assert len(policy.parsed) == 3
    assert all(source == "<memory>" for source, _, _ in policy.parsed)


def test_unverified_elements_cannot_bypass_policy_parsing(tmp_path):
    from pyxsd.resource_policy import ResourcePolicyError

    policy = RecordingPolicy(tmp_path)
    schema = pyxsd.compile(io.StringIO(SIMPLE), resource_policy=policy)
    with pytest.raises(ResourcePolicyError, match="Element"):
        schema.parse(ET.fromstring("<root>4</root>"))
    with pytest.raises(ResourcePolicyError, match="Element"):
        pyxsd.parse(ET.fromstring("<root>4</root>"), xsd="main.xsd", resource_policy=policy)
    assert policy.reads == []


def test_declaration_origins_are_captured_before_composition_rewrites(tmp_path):
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:main">'
        '<xs:include schemaLocation="part.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "part.xsd").write_text(SIMPLE, encoding="utf-8")
    policy = RecordingPolicy(tmp_path)
    schema = pyxsd.compile(tmp_path / "main.xsd", resource_policy=policy)
    declaration = policy.parsed[1][2].find(f"{{{XS}}}element")
    location = schema.declaration_origins[declaration]
    assert location.source == str(tmp_path / "part.xsd")
    assert [(step.name, step.index) for step in location.path] == [
        (f"{{{XS}}}schema", 1),
        (f"{{{XS}}}element", 1),
    ]


@pytest.mark.parametrize("operation", ["compile", "parse", "convenience"])
@pytest.mark.parametrize("content", [None, "<broken>"])
def test_policy_keeps_fatal_input_error_contract(tmp_path, operation, content):
    policy = RecordingPolicy(tmp_path)
    if content is not None:
        (tmp_path / "input.xml").write_text(content, encoding="utf-8")
    schema = pyxsd.compile(io.StringIO(SIMPLE), resource_policy=policy)
    with pytest.raises(pyxsd.PyXSDError):
        if operation == "compile":
            pyxsd.compile("input.xml", resource_policy=policy)
        elif operation == "parse":
            schema.parse("input.xml")
        else:
            pyxsd.parse("input.xml", resource_policy=policy)


@pytest.mark.parametrize("directive", ["include", "redefine", "override"])
def test_policy_source_identity_controls_nested_dependency_base(tmp_path, directive):
    (tmp_path / "real").mkdir()
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:{directive} schemaLocation="alias.xsd"/>'
        '<xs:element name="root" type="Number"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "real/types.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="value.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "real/value.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:simpleType name="Number">'
        '<xs:restriction base="xs:int"/></xs:simpleType></xs:schema>',
        encoding="utf-8",
    )

    class AliasPolicy(RecordingPolicy):
        def read(self, source, *, base, purpose):
            return super().read(
                "real/types.xsd" if source == "alias.xsd" else source, base=base, purpose=purpose
            )

    policy = AliasPolicy(tmp_path)
    schema = pyxsd.compile("main.xsd", resource_policy=policy)
    assert not schema.report.has_errors
    assert policy.reads[-1] == (
        tmp_path / "real/value.xsd",
        str(tmp_path / "real/types.xsd"),
        "schema",
    )


@pytest.mark.parametrize("cycle", [False, True])
def test_canonical_policy_source_ids_deduplicate_aliases(tmp_path, cycle):
    target = "main.xsd" if cycle else "types.xsd"
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="alias.xsd"/>'
        '<xs:include schemaLocation="alias2.xsd"/><xs:element name="root" type="xs:int"/>'
        "</xs:schema>",
        encoding="utf-8",
    )
    (tmp_path / "types.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:element name="extra" type="xs:int"/></xs:schema>',
        encoding="utf-8",
    )

    class AliasPolicy(RecordingPolicy):
        def read(self, source, *, base, purpose):
            return super().read(
                target if Path(source).name.startswith("alias") else source,
                base=base,
                purpose=purpose,
            )

    schema = pyxsd.compile("main.xsd", resource_policy=AliasPolicy(tmp_path))
    assert not schema.report.has_errors
    assert not schema.report.warnings


@pytest.mark.parametrize("absolute", [False, True])
def test_anonymous_stream_dependencies_never_default_to_cwd(tmp_path, absolute):
    from pyxsd.resource_policy import ResourcePolicyError

    (tmp_path / "types.xsd").write_text(SIMPLE, encoding="utf-8")
    location = str(tmp_path / "types.xsd") if absolute else "types.xsd"
    stream = io.StringIO(
        f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="{location}"/></xs:schema>'
    )
    policy = RecordingPolicy(tmp_path)
    if absolute:
        schema = pyxsd.compile(stream, resource_policy=policy)
        assert not schema.report.has_errors
        assert policy.reads == [(tmp_path / "types.xsd", None, "schema")]
    else:
        with pytest.raises(ResourcePolicyError, match="named source"):
            pyxsd.compile(stream, resource_policy=policy)
        assert policy.reads == []


def test_named_byte_streams_retain_source_and_original_encoding(tmp_path):
    policy = RecordingPolicy(tmp_path)
    schema_stream = io.BytesIO(SIMPLE.replace("xs:int", "xs:string").encode("utf-8"))
    schema_stream.name = str(tmp_path / "main.xsd")
    schema = pyxsd.compile(schema_stream, resource_policy=policy)
    content = '<?xml version="1.0" encoding="ISO-8859-1"?><root>caf\u00e9</root>'.encode(
        "iso-8859-1"
    )
    document = schema.parse(io.BytesIO(content))
    assert document.is_valid
    assert str(document.root) == "caf\u00e9"
    assert policy.parsed[0][0] == str(tmp_path / "main.xsd")
    assert policy.parsed[1][1] == content


def test_alias_cycle_of_an_active_dependency_is_bounded(tmp_path):
    (tmp_path / "main.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="part.xsd"/></xs:schema>',
        encoding="utf-8",
    )
    (tmp_path / "part.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:include schemaLocation="alias.xsd"/>'
        '<xs:element name="root" type="xs:int"/></xs:schema>',
        encoding="utf-8",
    )

    class AliasPolicy(RecordingPolicy):
        def read(self, source, *, base, purpose):
            return super().read(
                "part.xsd" if source == "alias.xsd" else source, base=base, purpose=purpose
            )

    policy = AliasPolicy(tmp_path)
    schema = pyxsd.compile("main.xsd", resource_policy=policy)
    assert not schema.report.has_errors
    assert [issue.code for issue in schema.report.warnings] == ["compose-cycle"]
    assert len(policy.reads) == 3


def test_default_named_stream_hint_resolution_keeps_legacy_cwd_behavior(tmp_path, monkeypatch):
    (tmp_path / "main.xsd").write_text(SIMPLE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    stream = io.StringIO(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="main.xsd">4</root>'
    )
    stream.name = str(tmp_path / "elsewhere/input.xml")
    assert pyxsd.parse(stream).is_valid


def test_anonymous_schema_namespace_mapping_cannot_use_cwd(tmp_path):
    from pyxsd.resource_policy import ResourcePolicyError

    policy = RecordingPolicy(tmp_path)
    with pytest.raises(ResourcePolicyError, match="named source"):
        pyxsd.compile(
            io.StringIO(SIMPLE),
            namespace_schemas={"urn:extra": "extra.xsd"},
            resource_policy=policy,
        )
    assert policy.reads == []


@pytest.mark.parametrize("absolute", [False, True])
def test_anonymous_instance_hints_use_the_policy_root_not_cwd(tmp_path, absolute):
    (tmp_path / "main.xsd").write_text(SIMPLE, encoding="utf-8")
    location = str(tmp_path / "main.xsd") if absolute else "main.xsd"
    stream = io.StringIO(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:noNamespaceSchemaLocation="{location}">4</root>'
    )
    policy = RecordingPolicy(tmp_path)
    document = pyxsd.parse(stream, resource_policy=policy)
    assert document.is_valid
    assert document.source is stream
    assert policy.reads == [(tmp_path / "main.xsd", None, "schema-hint")]


def test_advisory_instance_hint_denial_is_not_downgraded(tmp_path):
    from pyxsd.resource_policy import ResourcePolicyError

    (tmp_path / "main.xsd").write_text(SIMPLE, encoding="utf-8")
    (tmp_path / "input.xml").write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="urn:secret secret.xsd" '
        'xsi:noNamespaceSchemaLocation="main.xsd">4</root>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)
    policy.denied.add("secret.xsd")
    with pytest.raises(ResourcePolicyError, match="denied"):
        pyxsd.parse("input.xml", mode=pyxsd.ParseModes.NAMESPACED, resource_policy=policy)
    assert policy.reads[-1][0] == tmp_path / "secret.xsd"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"xml_policy": XmlParserPolicy.protected()},
        {"xml_limits": XmlLimits(max_bytes=1_000)},
    ],
)
def test_custom_resource_policy_owns_explicit_parser_arguments(tmp_path, kwargs):
    policy = RecordingPolicy(tmp_path)
    with pytest.raises(ValueError, match="resource_policy"):
        pyxsd.compile(io.StringIO(SIMPLE), resource_policy=policy, **kwargs)
    with pytest.raises(ValueError, match="resource_policy"):
        pyxsd.parse(
            io.StringIO("<root>4</root>"),
            xsd=io.StringIO(SIMPLE),
            resource_policy=policy,
            **kwargs,
        )
    schema = pyxsd.compile(io.StringIO(SIMPLE), resource_policy=policy)
    with pytest.raises(ValueError, match="resource_policy"):
        schema.parse(io.StringIO("<root>4</root>"), **kwargs)


def test_custom_policy_with_an_unrelated_compilation_attribute_compiles(tmp_path):
    """Only the private capability marker enables per-compilation scoping."""

    class NotesPolicy(RecordingPolicy):
        # Not a hook: a third-party policy may use this name for anything.
        compilation = "compiled once per schema"

    policy = NotesPolicy(tmp_path)
    schema = pyxsd.compile(io.StringIO(SIMPLE), resource_policy=policy)
    assert not schema.report.has_errors
    document = schema.parse(io.StringIO("<root>4</root>"))
    assert document.is_valid

    (tmp_path / "main.xsd").write_text(SIMPLE, encoding="utf-8")
    policy = NotesPolicy(tmp_path)
    schema = pyxsd.compile("main.xsd", resource_policy=policy)
    assert not schema.report.has_errors
    assert policy.reads[-1][2] == "schema"
