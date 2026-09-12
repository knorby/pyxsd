"""Transform revalidation-context tests (Phase 5).

Covers three defects in transform-driven revalidation:

* a transform's ``report`` attribute is absorbed without checking its
  type, so a custom transform using ``report`` for its own data crashed
  the run;
* absorbed reports were tracked by ``id()`` only, so once a report was
  collected a later report reusing its address was silently dropped;
* ``SendTreeToPyXSD`` revalidated against a stringified schema and lost
  the outer run's ``namespace_schemas``, so in-memory schemas and
  namespace-mapped imports failed to resolve.
"""

import io
import tempfile

import pytest

from conftest import fixture_dir, run_parser
from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.transforms import displayer as displayer_module
from pyxsd.transforms.send_tree_to_pyxsd import SendTreeToPyXSD
from pyxsd.validation import ValidationReport

XSD = str(fixture_dir("primitives") / "schema.xsd")
INSTANCE = fixture_dir("primitives") / "instance.xml"


def make_parser_and_invalid_tree():
    parser = run_parser("primitives")
    root = parser.schemaRootInstance
    root.count._value_ = ["not-an-int"]
    return parser, root


def codes(report):
    return [issue.code for issue in report]


def test_custom_transform_report_attribute_is_not_absorbed():
    """A transform may use ``report`` for its own data without crashing."""
    parser, _ = make_parser_and_invalid_tree()
    before = len(parser.report)
    parser._absorbTransformReport({"pointCount": 1})  # type: ignore[arg-type]
    assert len(parser.report) == before


def test_report_id_reuse_does_not_suppress_errors():
    """Absorbing reports by identity must survive id reuse.

    A clean report is absorbed and dropped; if its address is reused by
    later error reports, an id-only dedupe treats them as already
    absorbed. Every error must still reach the outer report.
    """
    parser, _ = make_parser_and_invalid_tree()
    first = ValidationReport()
    parser._absorbTransformReport(first)
    del first

    for _ in range(5):
        report = ValidationReport()
        report.add_error("invalid transformed tree", code="value")
        parser._absorbTransformReport(report)
        del report

    assert len([issue for issue in parser.report if issue.code == "value"]) == 5


def test_same_report_object_is_still_absorbed_once():
    parser, _ = make_parser_and_invalid_tree()
    inner = ValidationReport()
    inner.add_error("bad value", code="value")
    parser._absorbTransformReport(inner)
    parser._absorbTransformReport(inner)
    assert len([issue for issue in parser.report if issue.code == "value"]) == 1


class TestStreamSchemaInheritance:
    def test_in_memory_schema_is_reused(self, tmp_path, monkeypatch):
        """An outer run given a stream schema revalidates against it."""
        monkeypatch.chdir(tmp_path)
        schema_text = (fixture_dir("primitives") / "schema.xsd").read_text()
        parser = PyXSD(
            str(INSTANCE),
            io.StringIO(schema_text),
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=ParseModes.NAMESPACED,
        )
        transformer = SendTreeToPyXSD(parser.schemaRootInstance)
        transformer.outerParser = parser
        result = transformer()
        assert result is parser.schemaRootInstance
        assert transformer.report is not None
        assert "value" not in codes(transformer.report)


class TestNamespaceSchemaInheritance:
    def test_namespace_schemas_are_inherited(self, tmp_path, monkeypatch):
        """Imports resolved through namespace_schemas stay resolved."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "b.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
            ' targetNamespace="urn:b" elementFormDefault="qualified">'
            '  <xs:element name="item" type="xs:int"/>'
            "</xs:schema>"
        )
        (tmp_path / "main.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
            ' targetNamespace="urn:m" xmlns:b="urn:b"'
            ' elementFormDefault="qualified">'
            '  <xs:import namespace="urn:b"/>'
            '  <xs:element name="r"><xs:complexType><xs:sequence>'
            '    <xs:element ref="b:item"/>'
            "  </xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text(
            '<m:r xmlns:m="urn:m" xmlns:b="urn:b"><b:item>4</b:item></m:r>'
        )
        parser = PyXSD(
            str(tmp_path / "instance.xml"),
            str(tmp_path / "main.xsd"),
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
            mode=ParseModes.NAMESPACED,
            namespace_schemas={"urn:b": str(tmp_path / "b.xsd")},
        )
        assert not parser.report.has_errors

        transformer = SendTreeToPyXSD(parser.schemaRootInstance)
        transformer.outerParser = parser
        transformer()
        assert transformer.report is not None
        assert not transformer.report.has_errors


class TestSerializationFailureCleanup:
    def test_temp_stream_closes_when_serialization_fails(self, monkeypatch):
        """A failing tree write must not leak the temporary stream."""
        created = []
        real_tempfile = tempfile.TemporaryFile

        def recording_tempfile(*args, **kwargs):
            handle = real_tempfile(*args, **kwargs)
            created.append(handle)
            return handle

        class ExplodingWriter:
            def __init__(self, root, output):
                raise RuntimeError("cannot serialize")

        monkeypatch.setattr(tempfile, "TemporaryFile", recording_tempfile)
        monkeypatch.setattr(displayer_module, "XmlTreeWriter", ExplodingWriter)

        parser = run_parser("primitives")
        transformer = SendTreeToPyXSD(parser.schemaRootInstance)
        with pytest.raises(RuntimeError, match="cannot serialize"):
            transformer(xsdFile=XSD)

        assert created
        assert all(handle.closed for handle in created)
