"""The schemaless document interface: shared operations over a lexical tree."""

import io
import json

import pytest

from pyxsd.data_conversion.export import bound_to_data
from pyxsd.dict_export import bound_to_dict
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError
from pyxsd.schemaless import SchemalessDocument, SchemalessElement, parse_xml


def test_schemaless_document_uses_shared_document_operations():
    doc = parse_xml(io.StringIO("<r><c>1</c><c>2</c></r>"))
    assert isinstance(doc, Document)
    assert isinstance(doc, SchemalessDocument)
    assert doc.schema is None and doc.is_valid and doc.report_current
    assert [n._name_ for n in doc.walk()] == ["r", "c", "c"]
    assert [n.text_value() for n in doc.xpath("./c")] == ["1", "2"]
    assert doc.find("c") is doc.root.children[0]
    assert doc.findall("c") == [doc.root.children[0], doc.root.children[1]]
    assert doc.to_dict(typed=False) == {"c": ["1", "2"]}
    assert doc.to_json(typed=False) == '{"c": ["1", "2"]}'
    doc.require_valid()


def test_other_data_formats_are_explicit_capability_errors():
    with pytest.raises(PyXSDError, match="requires an XSD or RELAX NG"):
        parse_xml(io.StringIO("<r/>")).to_data(format="jsonml")


def test_native_typed_reads_and_export_keep_authoritative_lexical_strings():
    doc = parse_xml(io.StringIO('<r a="02138"><c>0012</c></r>'))

    assert doc.root.attribute_value("a") == "02138"
    assert doc.root.children[0].scalar_value() == "0012"
    assert doc.to_dict() == {"@a": "02138", "c": "0012"}
    assert doc.to_data() == {"@a": "02138", "c": "0012"}
    assert json.loads(doc.to_json()) == {"@a": "02138", "c": "0012"}


def test_export_reader_hooks_interpret_leaves_and_attributes():
    doc = parse_xml(io.StringIO('<r a="02138"><c>0012</c></r>'))
    readers = {
        "scalar_reader": lambda node: "S",
        "attribute_reader": lambda node, name: "A",
    }

    assert bound_to_dict(doc.root, **readers) == {"@a": "A", "c": "S"}
    assert bound_to_data(doc.root, **readers) == {"@a": "A", "c": "S"}


def test_schemaless_exports_use_the_lexical_reader_hooks(monkeypatch):
    doc = parse_xml(io.StringIO('<r a="02138"><c>0012</c></r>'))
    calls: list[tuple[str, str]] = []
    original_scalar = SchemalessElement.scalar_value
    original_attribute = SchemalessElement.attribute_value

    def scalar_value(node):
        calls.append(("scalar", node._name_))
        return original_scalar(node)

    def attribute_value(node, expanded_name):
        calls.append(("attribute", expanded_name))
        return original_attribute(node, expanded_name)

    monkeypatch.setattr(SchemalessElement, "scalar_value", scalar_value)
    monkeypatch.setattr(SchemalessElement, "attribute_value", attribute_value)

    assert doc.to_dict() == {"@a": "02138", "c": "0012"}
    assert calls == [("attribute", "a"), ("scalar", "c")]

    calls.clear()
    assert doc.to_data() == {"@a": "02138", "c": "0012"}
    assert calls == [("attribute", "a"), ("scalar", "c")]


def test_mixed_content_text_is_concatenated_in_document_order():
    doc = parse_xml(io.StringIO("<r>head<x/>tail<y>in</y>end</r>"))

    assert doc.to_dict(typed=False) == {"x": "", "y": "in", "$": "headtailend"}
    assert doc.to_dict() == {"x": None, "y": "in", "$": "headtailend"}


def test_always_list_wraps_every_child_value():
    doc = parse_xml(io.StringIO("<r><c>1</c><d/><d/></r>"))

    assert doc.to_dict(typed=False, always_list=True) == {"c": ["1"], "d": ["", ""]}
    assert doc.to_dict(always_list=True) == {"c": ["1"], "d": [None, None]}


def test_callable_transform_returning_a_lexical_node_wraps_a_stale_document():
    doc = parse_xml(io.StringIO("<r><c>1</c></r>"))
    child = doc.root.children[0]

    result = doc.transform(lambda root: root.children[0])

    assert isinstance(result, SchemalessDocument)
    assert result is not doc and result.root is child
    assert result.report is doc.report
    assert result.report_current is False
    assert result.source is None and result.source_index is None
    assert result._capture_source is None
    assert result.to_string() == "<c>1</c>"
    assert result.to_dict(typed=False) == {"$": "1"}


def test_callable_transform_returning_the_same_root_keeps_the_parse_capture():
    doc = parse_xml(io.StringIO("<r><c>1</c></r>"))

    result = doc.transform(lambda root: root)

    assert result is not doc and result.root is doc.root
    assert result.report_current is False
    assert result.source == doc.source
    assert result.source_index is doc.source_index
    assert result._capture_source is doc._capture_source


def test_transform_results_that_are_not_lexical_nodes_pass_through():
    doc = parse_xml(io.StringIO("<r><c>1</c></r>"))

    assert doc.transform(lambda root: 42) == 42
    assert doc.transform(lambda root, sep: sep.join([root._name_, "x"]), "-") == "r-x"


def test_lexical_mutation_makes_the_parse_report_stale():
    doc = parse_xml(io.StringIO("<r/>"))
    assert doc.report_current is True

    doc.root.set_text("edited")

    assert doc.report_current is False
    assert doc.is_valid  # the report is the parse-time snapshot, not refreshed


def test_revalidation_without_a_schema_is_an_explicit_capability_error():
    doc = parse_xml(io.StringIO("<r/>"))

    with pytest.raises(PyXSDError, match="no schema"):
        doc.revalidate()


def test_xdm_access_is_explicitly_unavailable_for_a_schemaless_parse():
    doc = parse_xml(io.StringIO("<r/>"))

    with pytest.raises(PyXSDError, match="XDM access is not connected"):
        doc.to_xdm()


def test_lexical_serialization_keeps_recorded_namespaces_and_mixed_content():
    source = '<r xmlns="urn:x" xmlns:p="urn:p" p:a="1">head<p:c/>tail</r>'
    doc = parse_xml(io.StringIO(source))

    assert doc.to_string() == source
    stream = io.StringIO()
    doc.write(stream)
    assert stream.getvalue() == source
    assert not stream.closed


def test_write_accepts_a_path(tmp_path):
    doc = parse_xml(io.StringIO("<r><c>1</c></r>"))
    destination = tmp_path / "out.xml"

    doc.write(destination)

    assert destination.read_text(encoding="utf-8") == "<r><c>1</c></r>"
