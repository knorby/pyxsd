"""The schemaless capability guide is executable and stays discoverable.

Every fenced Python block is run in a fresh namespace against its own inline
XML, JSON, or schema fixture. The assertions below then name the behaviors the
guide must demonstrate, so a prose-only rewrite cannot silently drop them.
"""

import io
import os
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"
GUIDE = DOCS / "schemaless-data.md"
DOC_REF = re.compile(r"\{doc\}`([^`]+)`")


def _python_blocks(markdown: str) -> list[str]:
    """Return every fenced ``python`` block in document order."""
    blocks = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].startswith("```python"):
            body = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    return blocks


def _load(token: str) -> dict:
    """Execute the one guide snippet containing *token* in a fresh namespace."""
    text = GUIDE.read_text(encoding="utf-8")
    for index, source in enumerate(_python_blocks(text)):
        if token in source:
            namespace: dict = {}
            exec(compile(source, f"schemaless-data.md[{index}]", "exec"), namespace)
            return namespace
    raise AssertionError(f"no documented snippet contains {token!r}")


# fmt: off
def test_schemaless_guide_covers_each_source_and_transform_route():
    text = Path("docs/schemaless-data.md").read_text(encoding="utf-8")
    for phrase in ("xs:untyped", "XdmMap", "XSLT 2.0", "format_assertion", "XmlMapping.compact", "OpenAPI"):
        assert phrase in text
    assert "replacement" not in text.lower()
# fmt: on


def test_every_documented_python_snippet_executes():
    blocks = _python_blocks(GUIDE.read_text(encoding="utf-8"))
    assert len(blocks) >= 10
    for index, source in enumerate(blocks):
        namespace: dict = {}
        exec(compile(source, f"schemaless-data.md[{index}]", "exec"), namespace)


def test_schemaless_xml_example_preserves_leading_zero_lexical_text():
    namespace = _load('attribute_lexical("id") == "02138"')
    orders = namespace["orders"]
    assert orders.root.children[0].attribute_lexical("id") == "02138"
    assert "02138" in orders.to_string()
    assert orders.schema is None


def test_failed_type_pin_is_reported_and_blocks_typed_reads():
    namespace = _load("pin_failure")
    assert namespace["report"].has_errors
    assert namespace["pin_failure"] is not None


def test_failed_pin_guide_qualifies_which_exports_stay_available():
    from pyxsd.schemaless import ScalarTypeError

    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert "default typed `to_dict()` and `to_data()` raise `ScalarTypeError`" in prose
    assert "native exports requested with `typed=False`" in prose
    assert "untyped XDM" in prose
    assert (
        "until a later `pin_type()` replaces the failing pin or the document is reparsed" in prose
    )
    assert "pin is removed" not in prose

    inferred = _load("pin_failure")["inferred"]
    assert inferred.root.children[1].text_value() == "true"
    assert inferred.to_data(typed=False) == {"id": "02138", "active": "true", "note": "12"}
    assert inferred.to_dict(typed=False) == {"id": "02138", "active": "true", "note": "12"}
    for export in (inferred.to_dict, inferred.to_data):
        with pytest.raises(ScalarTypeError):
            export()
    assert inferred.to_xdm() is not None

    assert not inferred.pin_type("/r/active", "xs:string").has_errors
    assert inferred.to_dict() == {"id": "02138", "active": "true", "note": 12}


def test_guide_distinguishes_xml_attribute_normalization_from_xsd_rules():
    from pyxsd.schemaless import parse_xml

    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert "applies no XSD whitespace normalization" in prose
    assert "XML's own attribute-value normalization still applies" in prose
    assert "normalize attribute values" not in prose

    document = parse_xml(io.StringIO('<r a="x\ny\tz" b="  padded  "/>'))
    assert document.root.attribute_lexical("a") == "x y z"
    assert document.root.attribute_lexical("b") == "  padded  "


def test_guide_qualifies_a_paired_singular_child_and_lists_api_entry_points():
    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert (
        "raises instead of choosing one unless the same class also declares the matching "
        "`children()` view" in prose
    )
    assert "Public signatures" not in prose
    assert "Public entry points and reference classes" in prose


def test_json_parse_error_guide_distinguishes_positions_from_value_failures():
    from pyxsd.schemaless import JsonParseError, from_json

    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert "`line`, `column`, and `position` are `None`" in prose
    assert "Value-model failures" in prose

    with pytest.raises(JsonParseError) as malformed:
        from_json('{"a": }')
    assert malformed.value.line is not None
    assert malformed.value.column is not None
    assert malformed.value.position is not None

    for source in ('{"a": 1, "a": 2}', '{"a": NaN}'):
        with pytest.raises(JsonParseError) as failure:
            from_json(source)
        assert failure.value.line is None
        assert failure.value.column is None
        assert failure.value.position is None


def test_profile_example_reports_occurrences_and_candidate_failures():
    namespace = _load('row.candidate("xs:integer").failed == 1')
    assert namespace["row"].samples == ("1", "2")
    assert namespace["profile"].complete is False


def test_native_json_example_keeps_exact_numbers_and_null():
    namespace = _load('payload.value_at("/total")')
    assert namespace["payload"].value_at("/flags/1") is None
    assert namespace["payload"].to_json() == (
        '{"id": "02138", "total": 10.50, "flags": [true, null]}'
    )


def test_native_json_example_uses_maps_and_arrays_as_xdm_items():
    from pyxsd.xdm import XdmArray, XdmMap

    namespace = _load("lines.size == 2")
    assert isinstance(namespace["root"], XdmMap)
    assert isinstance(namespace["lines"], XdmArray)
    assert namespace["lines"].size == 2
    assert namespace["rendered"] == "3"
    assert namespace["null_sequence"] == ()


def test_xslt_2_example_reads_the_explicit_xml_projection():
    namespace = _load('version="2.0"')
    assert namespace["document"].to_string() == '<order id="02138" total="10.5"/>'
    assert namespace["rendered"] == "02138"


def test_mapping_examples_show_element_first_compact_and_losses():
    namespace = _load("element_first.to_string()")
    assert namespace["compact"].root.attribute_lexical("id") == "02138"
    assert namespace["compact"].root.children[0].children[0].text_value() == "A"

    lossy = _load("null-attribute-omitted")["lossy"]
    assert lossy.is_valid
    assert lossy.report.warning_count == 1


def test_json_schema_example_reports_instances_and_format_assertion():
    namespace = _load('invalid.report.issues[0].location.pointer == "/id"')
    assert namespace["valid"].is_valid
    assert not namespace["invalid"].is_valid
    assert namespace["invalid"].to_data() == {"id": 7, "total": "10.50"}

    formats = _load("annotated.is_valid")
    assert formats["annotated"].is_valid
    assert not formats["asserted"].is_valid


def test_guide_documents_deterministic_rfc3339_format_assertion():
    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert "pyxsd uses its own deterministic RFC 3339 checks" in prose
    assert "for `date`, `date-time`, and `time`" in prose
    assert "do not require `rfc3339-validator`" in prose
    assert "never casts a formatted string without an explicit `TypePolicy`" in prose
    assert "RFC 3339 formats still do not cast automatically" in prose


def test_guide_names_every_deterministic_format_and_python_regex_boundary():
    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    for name in (
        "uuid",
        "ipv4",
        "ipv6",
        "json-pointer",
        "regex",
        "int32",
        "int64",
        "float",
        "double",
        "byte",
    ):
        assert f"`{name}`" in prose
    assert "Python's `re` syntax rather than ECMA-262" in prose
    assert (
        "unlisted formats remain annotations whose assertion depends on the installed "
        "`jsonschema` environment" in prose
    )
    assert "complete Format-Assertion vocabulary claim" in prose

    namespace = _load("python_pattern.is_valid")
    assert namespace["annotated_pointer"].is_valid
    assert not namespace["asserted_pointer"].is_valid
    assert namespace["python_pattern"].is_valid


def test_guide_separates_format_assertion_from_format_typing():
    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert "`format_typing=True` is a separate opt-in" in prose
    assert "never changes native storage, and never changes `to_data()` or `to_json()`" in prose
    assert "Explicit `TypePolicy` pins and `MappingRule.type` always win" in prose

    namespace = _load('"not-a-uuid"')
    assert [document.is_valid for document in namespace["results"]] == [True, True, False, False]
    assert [document.value_at("", typed=True) for document in namespace["results"]] == [
        "not-a-uuid"
    ] * 4


def test_guide_maps_uuid_and_ip_formats_to_stdlib_values_with_string_xdm():
    from ipaddress import IPv4Address, IPv6Address
    from uuid import UUID

    from pyxsd.xdm import XdmAtomic

    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert "`uuid.UUID`" in prose
    assert "`ipaddress.IPv4Address`" in prose
    assert "`ipaddress.IPv6Address`" in prose
    assert "canonical `xs:string`" in prose

    address_typed = _load("address_typed")["address_typed"]
    assert address_typed.value_at("/id", typed=True) == UUID("123e4567-e89b-12d3-a456-426614174000")
    assert address_typed.value_at("/host", typed=True) == IPv4Address("192.0.2.1")
    assert address_typed.value_at("/peer", typed=True) == IPv6Address("2001:db8::1")
    root = address_typed.to_xdm()[0]
    for name, canonical in (
        ("id", "123e4567-e89b-12d3-a456-426614174000"),
        ("host", "192.0.2.1"),
        ("peer", "2001:db8::1"),
    ):
        (atomic,) = root.get(XdmAtomic.string(name))
        assert atomic.type_name == "{http://www.w3.org/2001/XMLSchema}string"
        assert atomic.value == canonical


def test_guide_maps_openapi_formats_to_simple_types_in_projection():
    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    for phrase in (
        "`int32` becomes `xs:int`",
        "`int64` becomes `xs:long`",
        "`float` becomes `xs:float`",
        "`double` becomes `xs:double`",
        "`byte` or `contentEncoding: base64` becomes `xs:base64Binary`",
    ):
        assert phrase in prose

    namespace = _load("format_measurement")
    assert namespace["native_format_xml"].to_string() == (
        "<measurement><count>1.0</count><total>9223372036854775807.0</total>"
        "<ratio>16777217</ratio><score>0.125</score><payload>YQ==</payload></measurement>"
    )
    assert namespace["typed_format_xml"].to_string() == (
        "<measurement><count>1</count><total>9223372036854775807</total>"
        "<ratio>16777216.0</ratio><score>0.125</score><payload>YQ==</payload></measurement>"
    )


def test_guide_documents_string_int64_legacy_byte_and_no_automatic_decode():
    prose = " ".join(GUIDE.read_text(encoding="utf-8").split())
    assert (
        "`int64` accepts a JSON number or a string that matches an optional `-` plus ASCII digits"
        in prose
    )
    assert "`byte` is the legacy OpenAPI 3.0 spelling" in prose
    assert "prefer `contentEncoding: base64`" in prose
    assert "never decodes base64 automatically" in prose

    namespace = _load("encoded_package")
    assert namespace["encoded_package"].value_at("/payload") == "SGVsbG8="
    assert namespace["encoded_xml"].to_string() == "<package><payload>SGVsbG8=</payload></package>"


def test_openapi_example_maps_a_property_to_an_attribute():
    namespace = _load('xml.root.attribute_lexical("id") == "02138"')
    xml = namespace["xml"]
    assert xml.root.attribute_lexical("id") == "02138"
    assert xml.root.text_value() == "hold"
    assert [node._name_ for node in xml.root.children] == ["line", "line"]


def test_xdm_fidelity_example_stales_xdm_and_revalidates_the_projection():
    namespace = _load("refreshed = doc.revalidate()")
    assert namespace["stale_error"] is not None
    assert namespace["refreshed"].to_string() == "<order><line>B</line></order>"
    assert "<!--" not in namespace["refreshed"].to_string()


def test_base_revalidation_docs_distinguish_the_schemaless_refresh():
    from inspect import getdoc

    from pyxsd.document import Document

    prose = getdoc(Document.revalidate)
    assert "Document(schema=None)" in prose
    assert "SchemalessDocument.revalidate" in prose


def test_guide_documents_the_numeric_contract_of_parse_json():
    text = GUIDE.read_text(encoding="utf-8")
    for phrase in ("parse-json", "xs:double", "Decimal"):
        assert phrase in text


def test_guide_documents_fidelity_security_and_scope_boundaries():
    text = GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "root null",
        "source-complete",
        "conservative inference",
        "warning",
        "reference_registry",
        "resource_policy",
        "already-loaded Schema Object",
        "format_assertion",
    ):
        assert phrase in text
    assert "pyxsd[jsonschema]" in text


def test_guide_examples_stay_outside_mcp_and_schema_synthesis():
    for source in _python_blocks(GUIDE.read_text(encoding="utf-8")):
        lowered = source.lower()
        assert "mcp" not in lowered
        assert "stub(" not in lowered


def test_guide_is_included_in_a_sphinx_toctree():
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    toctrees = re.findall(r"```\{toctree\}\n(.*?)```", index, re.S)
    assert any("schemaless-data" in block.split() for block in toctrees)


def test_navigation_pages_link_to_the_guide():
    for name in ("index.md", "api.md", "data-workflows.md", "integrations.md"):
        assert "{doc}`schemaless-data`" in (DOCS / name).read_text(encoding="utf-8")


def test_every_doc_reference_in_the_guide_resolves():
    text = GUIDE.read_text(encoding="utf-8")
    targets = DOC_REF.findall(text)
    assert targets
    for target in targets:
        name, _, anchor = target.partition("#")
        assert (DOCS / f"{name}.md").is_file(), target
        if anchor:
            assert f"({anchor})=" in (DOCS / f"{name}.md").read_text(encoding="utf-8"), target


def test_every_relative_markdown_link_in_the_guide_resolves():
    text = GUIDE.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)", text):
        assert (DOCS / target).is_file(), target


def test_guide_is_present_in_a_built_docs_site():
    build = os.environ.get("PYXSD_DOCS_BUILD")
    if not build:
        pytest.skip("set PYXSD_DOCS_BUILD to a freshly built HTML directory")
    assert (Path(build) / "schemaless-data.html").is_file()
