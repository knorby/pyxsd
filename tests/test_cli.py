"""End-to-end tests for the command line interface and its helpers."""

import shutil
import xml.etree.ElementTree as ET

import pytest

from conftest import assert_xml_canonically_equal, fixture_dir, run_parser
from pyxsd.parser import (
    PyXSD,
    _transformModuleNames,
    main,
    parseTransformCall,
)


def stage_fixture(tmp_path, fixture):
    """Copy a fixture's schema and instance into tmp_path."""
    for name in ("schema.xsd", "instance.xml"):
        shutil.copy(fixture_dir(fixture) / name, tmp_path / name)
    return tmp_path / "instance.xml"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "PyXSD" in capsys.readouterr().out


def test_parsed_output_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-k", "-o", "/dev/null"])
    parsed = tmp_path / "instanceParsed.xml"
    assert parsed.exists()
    assert_xml_canonically_equal(fixture_dir("inventory") / "instance.xml", parsed)


def test_explicit_parsed_output_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-k", "-p", "out.xml", "-o", "/dev/null"])
    assert (tmp_path / "out.xml").exists()


def test_transform_to_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-t", "PrintData()"])
    out = capsys.readouterr().out
    assert "<inventory" in out
    assert "wrench" in out


def test_transform_chain_to_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-t", "PrintData() > PrintData()"])
    out = capsys.readouterr().out
    # Each PrintData writes the tree itself, and the pipeline then
    # writes the (transformed) root once more to the transform output:
    # 2 self-writes + 1 final write.
    assert out.count("<inventory") == 3


def test_transform_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-t", "PrintData()", "-o", "transformed.xml"])
    transformed = tmp_path / "transformed.xml"
    assert transformed.exists()
    assert_xml_canonically_equal(fixture_dir("inventory") / "instance.xml", transformed)


def test_transform_default_output_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-t", "PrintData()", "-d"])
    assert (tmp_path / "instanceTransformed.xml").exists()


def test_transform_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    transform_file = tmp_path / "transforms.txt"
    transform_file.write_text("PrintData()\n")
    main(["-i", "instance.xml", "-T", str(transform_file), "-o", "out.xml"])
    assert (tmp_path / "out.xml").exists()


def _instance_without_hints(tmp_path, fixture):
    """Stage a fixture instance whose schema-location hints are removed."""
    stage_fixture(tmp_path, fixture)
    instance = tmp_path / "instance.xml"
    content = instance.read_text()
    stripped = content.replace('xsi:noNamespaceSchemaLocation="schema.xsd"', "")
    instance.write_text(stripped)
    return instance


def test_explicit_schema_flag(tmp_path, monkeypatch):
    """-s works even when the instance carries no schema hints."""
    monkeypatch.chdir(tmp_path)
    _instance_without_hints(tmp_path, "inventory")
    main(["-i", "instance.xml", "-s", "schema.xsd", "-k", "-o", "/dev/null"])
    parsed = tmp_path / "instanceParsed.xml"
    assert parsed.exists()


def test_missing_schema_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _instance_without_hints(tmp_path, "inventory")
    with pytest.raises(ValueError, match="no schema file"):
        main(["-i", "instance.xml", "-o", "/dev/null"])


def test_transform_call_and_file_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", "instance.xml", "-t", "PrintData()", "-T", "nope.txt"])
    assert excinfo.value.code == 2


def test_verbose_and_quiet_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", "instance.xml", "-v", "-q"])
    assert excinfo.value.code == 2


def test_unknown_transform_module_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    with pytest.raises(ImportError, match="NoSuchTransform"):
        main(["-i", "instance.xml", "-t", "NoSuchTransform()"])


# ---------------------------------------------------------------------------
# Transform call parsing and module-name derivation
# ---------------------------------------------------------------------------


class TestParseTransformCall:
    def test_bare_call(self):
        assert parseTransformCall("PrintData()") == ("PrintData", [], {})

    def test_positional_arguments(self):
        assert parseTransformCall('T(1, "two", 3.5)') == ("T", [1, "two", 3.5], {})

    def test_keyword_arguments(self):
        assert parseTransformCall("T(a=1, b='x')") == ("T", [], {"a": 1, "b": "x"})

    def test_mixed_and_literals(self):
        assert parseTransformCall("T(None, True, [1, 2], k=('a',))") == (
            "T",
            [None, True, [1, 2]],
            {"k": ("a",)},
        )

    def test_bare_class_name_is_rejected(self):
        with pytest.raises(ValueError, match="correct syntax"):
            parseTransformCall("PrintData")

    def test_syntax_error_is_rejected(self):
        with pytest.raises(ValueError, match="correct syntax"):
            parseTransformCall("PrintData((")

    def test_non_literal_argument_is_rejected(self):
        with pytest.raises(ValueError):
            parseTransformCall("T(some_name)")

    def test_attribute_access_is_rejected(self):
        with pytest.raises(ValueError, match="correct syntax"):
            parseTransformCall("mod.PrintData()")


class TestTransformModuleNames:
    def test_single_word(self):
        assert _transformModuleNames("PrintData") == ["printData", "print_data"]

    def test_multi_word(self):
        # The generic camel->snake conversion splits acronym runs, so
        # 'PyXSD' becomes 'py_xsd'; module resolution compensates with
        # an underscore-insensitive fallback (see
        # test_transform_module_load_acronym_fallback).
        assert _transformModuleNames("SendTreeToPyXSD") == [
            "sendTreeToPyXSD",
            "send_tree_to_py_xsd",
        ]

    def test_no_case_conversion_collapses_to_one(self):
        assert _transformModuleNames("Foo") == ["foo"]

    def test_transform_module_load_acronym_fallback(self):
        """Acronym-split names still resolve to the shipped module."""
        parser = run_parser("inventory")
        module = parser.getTransformModuleAndLoad("SendTreeToPyXSD")
        assert hasattr(module, "SendTreeToPyXSD")


class TestDefaultFileNames:
    def test_parsed_output_name(self, tmp_path):
        parser = run_parser("inventory")
        assert parser.getXmlOutputFileName().endswith("instanceParsed.xml")

    def test_transforms_output_name(self, tmp_path):
        parser = run_parser("inventory")
        assert parser.getTransformsFileName().endswith("instanceTransformed.xml")


def _unparsed_root(xml_text, tmp_path):
    """Build a PyXSD with only ``xmlRoot`` set, bypassing the pipeline.

    ``getSchemaInfo`` is a pure function of the parsed root element;
    the full constructor consumes/rewrites the hint attributes, so the
    schema-info tests inspect it on an untouched root.
    """
    source = tmp_path / "probe.xml"
    source.write_text(xml_text)
    parser = PyXSD.__new__(PyXSD)
    parser.xmlRoot = ET.parse(source).getroot()
    return parser


class TestSchemaInfo:
    def test_no_namespace_schema_location(self, tmp_path):
        parser = _unparsed_root(
            '<inventory xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:noNamespaceSchemaLocation="schema.xsd"/>',
            tmp_path,
        )
        assert parser.getSchemaInfo("l") == "schema.xsd"
        assert parser.getSchemaInfo("n") is None
        assert parser.getSchemaInfo("t").endswith("noNamespaceSchemaLocation")

    def test_namespace_schema_location(self, tmp_path):
        parser = _unparsed_root(
            '<inventory xmlns="http://example.com/ns"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:schemaLocation="http://example.com/ns schema.xsd"/>',
            tmp_path,
        )
        assert parser.getSchemaInfo("l") == "schema.xsd"
        assert parser.getSchemaInfo("n") == "http://example.com/ns"
        assert parser.getSchemaInfo("t").endswith("schemaLocation")

    def test_no_hints_returns_none(self, tmp_path):
        parser = _unparsed_root("<inventory/>", tmp_path)
        assert parser.getSchemaInfo("l") is None
        assert parser.getSchemaInfo("n") is None
