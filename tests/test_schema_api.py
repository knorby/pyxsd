"""Tests for the pyxsd.Schema object API."""

from io import BytesIO, StringIO

import pytest

import pyxsd
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note" type="Note"/>
  <xs:complexType name="Note">
    <xs:sequence>
      <xs:element name="to" type="xs:string"/>
      <xs:element name="from" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""

BROKEN = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="a">
    <xs:complexType>
      <xs:complexContent>
        <xs:extension base="NoSuchType"/>
      </xs:complexContent>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

NOT_A_SCHEMA = "<root/>"


def test_compile_returns_schema_with_classes():
    schema = pyxsd.Schema.compile(StringIO(SCHEMA))
    assert "Note" in schema.classes
    assert "note" in schema.classes or schema.classes.get("schema") is not None


def test_compile_collects_schema_phase_report_not_raise():
    schema = pyxsd.Schema.compile(StringIO(BROKEN))
    assert schema.report.has_errors


def test_require_valid_raises_validation_error():
    schema = pyxsd.Schema.compile(StringIO(BROKEN))
    with pytest.raises(pyxsd.ValidationError) as excinfo:
        schema.require_valid()
    assert excinfo.value.report is schema.report


def test_require_valid_passes_clean_schema():
    schema = pyxsd.Schema.compile(StringIO(SCHEMA))
    schema.require_valid()  # must not raise


def test_compile_fatal_inputs_raise_pyxsd_error():
    with pytest.raises(pyxsd.PyXSDError):
        pyxsd.Schema.compile(StringIO(NOT_A_SCHEMA))
    with pytest.raises(pyxsd.PyXSDError):
        pyxsd.Schema.compile("/no/such/file/anywhere.xsd")


def test_target_namespace_exposed():
    ns_schema = SCHEMA.replace(
        'xmlns:xs="http://www.w3.org/2001/XMLSchema">',
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:t">',
    )
    schema = pyxsd.Schema.compile(StringIO(ns_schema))
    assert schema.target_namespace == "urn:t"


HINT_DOC = (
    '<?xml version="1.0"?>'
    '<note xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="note.xsd">'
    "<to>you</to><count>1</count></note>"
)


def test_package_parse_resolves_hint(tmp_path):
    (tmp_path / "note.xsd").write_text(SCHEMA)
    (tmp_path / "note.xml").write_text(HINT_DOC)
    doc = pyxsd.parse(tmp_path / "note.xml")
    assert doc.root is not None


def test_package_parse_without_schema_raises():
    with pytest.raises(pyxsd.PyXSDError):
        pyxsd.parse(StringIO("<x/>"))


def test_package_parse_missing_file_raises_pyxsd_error():
    with pytest.raises(pyxsd.PyXSDError, match="could not be read"):
        pyxsd.parse("/no/such/file/anywhere.xml")


def test_package_parse_malformed_xml_raises_pyxsd_error(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<root><child></root>")
    with pytest.raises(pyxsd.PyXSDError, match="could not be read"):
        pyxsd.parse(bad)


def test_package_compile_alias():
    schema = pyxsd.compile(StringIO(SCHEMA))
    assert isinstance(schema, pyxsd.Schema)


XS = "http://www.w3.org/2001/XMLSchema"
SIMPLE_STRING = f'<xs:schema xmlns:xs="{XS}"><xs:element name="root" type="xs:string"/></xs:schema>'
ENTITY_SCHEMA = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE xs:schema [<!ENTITY ns "urn:unit">]>'
    f'<xs:schema xmlns:xs="{XS}" targetNamespace="&ns;">'
    '<xs:element name="root" type="xs:string"/>'
    "</xs:schema>"
)
PLAIN_DTD_SCHEMA = f'<?xml version="1.0"?><!DOCTYPE xs:schema>{SCHEMA.split("?>", 1)[1]}'
ENTITY_INSTANCE = b'<!DOCTYPE root [<!ENTITY value "4">]><root>&value;</root>'
LIST_SCHEMA = (
    f'<xs:schema xmlns:xs="{XS}"><xs:element name="list"><xs:complexType><xs:sequence>'
    '<xs:element name="item" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>'
    "</xs:sequence></xs:complexType></xs:element></xs:schema>"
)


class TrustedPolicy:
    """A caller-supplied resource policy: its parse() owns XML behavior."""

    def read(self, source, *, base, purpose):
        raise AssertionError("read must not run when parser arguments conflict")

    def parse(self, data, context):
        from xml.etree import ElementTree as ET

        return ET.fromstring(data.content)


def test_compile_protected_policy_rejects_main_schema_entities():
    with pytest.raises(ResourcePolicyError):
        pyxsd.Schema.compile(
            StringIO(ENTITY_SCHEMA),
            xml_policy=XmlParserPolicy.protected(),
        )


def test_compile_limits_accept_a_schema_inside_the_budget():
    schema = pyxsd.Schema.compile(
        StringIO(SCHEMA),
        xml_limits=XmlLimits(max_bytes=1_000),
    )
    assert not schema.report.has_errors


def test_compile_limits_reject_the_main_schema_over_budget():
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        pyxsd.Schema.compile(StringIO(SCHEMA), xml_limits=XmlLimits(max_bytes=10))


def test_compile_default_profile_still_expands_schema_entities():
    schema = pyxsd.Schema.compile(StringIO(ENTITY_SCHEMA))
    assert schema.target_namespace == "urn:unit"


def test_compile_strict_profile_rejects_a_plain_schema_doctype():
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        pyxsd.Schema.compile(StringIO(PLAIN_DTD_SCHEMA), xml_policy=XmlParserPolicy.strict())


@pytest.mark.parametrize("directive", ["include", "import"])
def test_compile_policy_applies_to_included_and_imported_schemas(tmp_path, directive):
    imported = directive == "import"
    namespace = ' namespace="urn:part"' if imported else ""
    main = (
        f'<xs:schema xmlns:xs="{XS}" xmlns:p="urn:part">'
        f'<xs:{directive}{namespace} schemaLocation="part.xsd"/>'
        '<xs:element name="root" type="xs:string"/>'
        "</xs:schema>"
    )
    part = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE xs:schema [<!ENTITY tns "urn:part">]>'
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="&tns;">'
        '<xs:simpleType name="Number"><xs:restriction base="xs:int"/></xs:simpleType>'
        "</xs:schema>"
    )
    (tmp_path / "main.xsd").write_text(main, encoding="utf-8")
    (tmp_path / "part.xsd").write_text(part, encoding="utf-8")
    with pytest.raises(ResourcePolicyError):
        pyxsd.compile(tmp_path / "main.xsd", xml_policy=XmlParserPolicy.protected())


def test_schema_parse_policy_and_limits_apply_to_the_instance():
    schema = pyxsd.Schema.compile(StringIO(SIMPLE_STRING))
    with pytest.raises(ResourcePolicyError):
        schema.parse(BytesIO(ENTITY_INSTANCE), xml_policy=XmlParserPolicy.protected())
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        schema.parse(BytesIO(b"<root>4</root>"), xml_limits=XmlLimits(max_bytes=5))


def test_schema_parse_default_profile_still_expands_instance_entities():
    schema = pyxsd.Schema.compile(StringIO(SIMPLE_STRING))
    document = schema.parse(BytesIO(ENTITY_INSTANCE))
    assert document.is_valid
    assert str(document.root) == "4"


def test_module_parse_policy_applies_to_instance_and_schema(tmp_path):
    (tmp_path / "schema.xsd").write_text(SIMPLE_STRING, encoding="utf-8")
    policy = XmlParserPolicy.protected()
    document = pyxsd.parse(
        BytesIO(b"<root>4</root>"),
        xsd=tmp_path / "schema.xsd",
        xml_policy=policy,
    )
    assert document.is_valid
    with pytest.raises(ResourcePolicyError):
        pyxsd.parse(
            BytesIO(ENTITY_INSTANCE),
            xsd=tmp_path / "schema.xsd",
            xml_policy=policy,
        )
    entity_schema = tmp_path / "entity.xsd"
    entity_schema.write_text(ENTITY_SCHEMA, encoding="utf-8")
    with pytest.raises(ResourcePolicyError):
        pyxsd.parse(BytesIO(b"<root>4</root>"), xsd=entity_schema, xml_policy=policy)
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        pyxsd.parse(
            BytesIO(b"<root>4</root>"),
            xsd=tmp_path / "schema.xsd",
            xml_limits=XmlLimits(max_bytes=3),
        )


def test_advisory_schema_hint_policy_denial_is_not_downgraded(tmp_path):
    (tmp_path / "main.xsd").write_text(SIMPLE_STRING, encoding="utf-8")
    (tmp_path / "hint.xsd").write_text(ENTITY_SCHEMA, encoding="utf-8")
    (tmp_path / "input.xml").write_text(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="urn:unit hint.xsd" '
        'xsi:noNamespaceSchemaLocation="main.xsd">4</root>',
        encoding="utf-8",
    )
    with pytest.raises(ResourcePolicyError):
        pyxsd.parse(
            tmp_path / "input.xml",
            mode=pyxsd.ParseModes.NAMESPACED,
            xml_policy=XmlParserPolicy.protected(),
        )


def test_cli_propagates_the_hint_selected_schema_purpose(tmp_path, monkeypatch):
    from pyxsd import cli
    from pyxsd.schema import Schema

    (tmp_path / "note.xsd").write_text(SCHEMA)
    (tmp_path / "note.xml").write_text(HINT_DOC)
    seen = []
    original = Schema.compile.__func__

    def recording(cls, source, **kwargs):
        seen.append(kwargs.get("_source_purpose"))
        return original(cls, source, **kwargs)

    monkeypatch.setattr(Schema, "compile", classmethod(recording))
    document = cli._build_document(
        str(tmp_path / "note.xml"), None, mode=pyxsd.ParseModes.STRICT, overlay=None
    )
    assert seen == ["schema-hint"]
    assert document.root is not None

    explicit = cli._build_document(
        str(tmp_path / "note.xml"),
        str(tmp_path / "note.xsd"),
        mode=pyxsd.ParseModes.STRICT,
        overlay=None,
    )
    assert seen == ["schema-hint", "schema"]
    assert explicit.root is not None


def test_compile_policy_and_limits_do_not_leak_into_later_parses():
    schema = pyxsd.Schema.compile(
        StringIO(SIMPLE_STRING),
        xml_policy=XmlParserPolicy.protected(),
    )
    document = schema.parse(BytesIO(ENTITY_INSTANCE))
    assert document.is_valid

    limited = pyxsd.Schema.compile(
        StringIO(LIST_SCHEMA),
        xml_limits=XmlLimits(max_elements=5),
    )
    document = limited.parse(BytesIO(b"<list>" + b"<item>x</item>" * 6 + b"</list>"))
    assert document.is_valid


@pytest.mark.parametrize(
    "kwargs",
    [
        {"xml_policy": XmlParserPolicy.protected()},
        {"xml_limits": XmlLimits(max_bytes=4)},
    ],
)
def test_resource_policy_rejects_explicit_parser_arguments(kwargs):
    trusted = TrustedPolicy()
    with pytest.raises(ValueError, match="resource_policy"):
        pyxsd.compile(StringIO(SCHEMA), resource_policy=trusted, **kwargs)
    with pytest.raises(ValueError, match="resource_policy"):
        pyxsd.parse(StringIO(HINT_DOC), xsd=StringIO(SCHEMA), resource_policy=trusted, **kwargs)
    schema = pyxsd.Schema.compile(StringIO(SIMPLE_STRING), resource_policy=trusted)
    with pytest.raises(ValueError, match="resource_policy"):
        schema.parse(StringIO("<root>4</root>"), **kwargs)
