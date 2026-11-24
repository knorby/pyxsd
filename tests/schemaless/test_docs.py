"""The schemaless capability guide is executable and stays discoverable.

Every fenced Python block is run in a fresh namespace against its own inline
XML, JSON, or schema fixture. The assertions below then name the behaviors the
guide must demonstrate, so a prose-only rewrite cannot silently drop them.
"""

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

    inferred = _load("pin_failure")["inferred"]
    assert inferred.root.children[1].text_value() == "true"
    assert inferred.to_data(typed=False) == {"id": "02138", "active": "true", "note": "12"}
    assert inferred.to_dict(typed=False) == {"id": "02138", "active": "true", "note": "12"}
    for export in (inferred.to_dict, inferred.to_data):
        with pytest.raises(ScalarTypeError):
            export()
    assert inferred.to_xdm() is not None


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
