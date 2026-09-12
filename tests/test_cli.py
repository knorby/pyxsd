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
    split_transform_chain,
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


def test_missing_schema_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _instance_without_hints(tmp_path, "inventory")
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", "instance.xml", "-o", "/dev/null"])
    assert excinfo.value.code == 1
    assert "no schema file" in capsys.readouterr().err


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


def test_unknown_transform_prints_clean_error(tmp_path, monkeypatch, capsys):
    """A missing transform is a usage error, not a traceback."""
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", "instance.xml", "-t", "NoSuchTransform()"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "NoSuchTransform" in err
    assert "Traceback" not in err


def test_transform_bad_signature_prints_clean_error(tmp_path, monkeypatch, capsys):
    """Arguments that do not match the transform signature are reported."""
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", "instance.xml", "-t", "PrintData(bogus=True)"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "PrintData" in err
    assert "bogus" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Validation reporting and exit codes
# ---------------------------------------------------------------------------


def _invalid_instance(tmp_path, fixture="nested"):
    """Stage a fixture instance with its required attribute removed."""
    stage_fixture(tmp_path, fixture)
    instance = tmp_path / "instance.xml"
    instance.write_text(instance.read_text().replace(' id="p-1"', ""))
    return instance


def test_validation_report_goes_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _invalid_instance(tmp_path)
    main(["-i", "instance.xml", "-o", "/dev/null"])
    err = capsys.readouterr().err
    assert "missing-attribute" in err
    assert "required but was not found" in err


def test_strict_flag_exits_nonzero_on_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _invalid_instance(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", "instance.xml", "-o", "/dev/null", "--strict"])
    assert excinfo.value.code == 1
    assert "missing-attribute" in capsys.readouterr().err


def test_strict_flag_passes_valid_instance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-o", "/dev/null", "--strict"])


def test_valid_instance_prints_no_report(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    main(["-i", "instance.xml", "-o", "/dev/null"])
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Stdin input
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self, text, tty=False):
        self._text = text
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self):
        return self._text


def test_stdin_input_without_temporary_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    xml = fixture_dir("inventory").joinpath("instance.xml").read_text()
    monkeypatch.setattr("sys.stdin", _FakeStdin(xml))
    main(["-s", str(fixture_dir("inventory") / "schema.xsd"), "-o", "/dev/null"])
    # The historical behavior wrote a 'stdin.xml' temporary file to the
    # working directory; nothing may be created now.
    assert list(tmp_path.iterdir()) == []


def test_stdin_tty_is_rejected(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin("", tty=True))
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Overlay classes (experimental)
# ---------------------------------------------------------------------------


def test_overlay_class_file_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    overlay = tmp_path / "overlay.py"
    overlay.write_text(
        "from pyxsd.schema_base import SchemaBase\n"
        "\n"
        "\n"
        "class ExtraType(SchemaBase):\n"
        "    name = 'unusedType'\n"
    )
    rc = main(["-i", "instance.xml", "-c", "overlay.py", "-o", "/dev/null"])
    assert rc is None
    # The overlay module is loaded by name from the working directory.
    assert overlay.exists()


def test_overlay_class_without_name_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    overlay = tmp_path / "overlay.py"
    overlay.write_text(
        "from pyxsd.schema_base import SchemaBase\n\n\nclass Nameless(SchemaBase):\n    pass\n"
    )
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        main(["-i", "instance.xml", "-c", "overlay.py", "-o", "/dev/null"])
    assert any(issubclass(w.category, UserWarning) for w in caught)


def test_missing_overlay_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    with pytest.raises(ImportError, match="was not found"):
        main(["-i", "instance.xml", "-c", "nope.py", "-o", "/dev/null"])


def test_malformed_schema_location_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    instance = tmp_path / "instance.xml"
    content = instance.read_text().replace(
        'xsi:noNamespaceSchemaLocation="schema.xsd"',
        'xmlns:test="http://example.com/ns" xsi:schemaLocation="http://example.com/ns"',
    )
    instance.write_text(content)
    main(["-i", "instance.xml", "-s", "schema.xsd", "-o", "/dev/null"])
    err = capsys.readouterr().err
    assert "schema-hint" in err


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

    def test_kwargs_expansion_is_rejected(self):
        with pytest.raises(ValueError, match="does not use correct syntax"):
            parseTransformCall("T(**opts)")

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


class TestSplitTransformChain:
    def test_simple_chain(self):
        assert split_transform_chain("A()>B()") == ["A()", "B()"]

    def test_single_call_has_no_separator(self):
        assert split_transform_chain("PrintData()") == ["PrintData()"]

    def test_quoted_separator_is_kept(self):
        chain = 'PrintData("a>b.xml")'
        assert split_transform_chain(chain) == [chain]

    def test_quoted_separator_between_calls(self):
        assert split_transform_chain('A("x>y")>B()') == ['A("x>y")', "B()"]

    def test_separator_inside_nested_call(self):
        assert split_transform_chain('A(f(">"))>B()') == ['A(f(">"))', "B()"]

    def test_multiline_triple_quoted_argument(self):
        """Token positions are absolute, not line-relative columns."""
        chain = 'A("""x\ny""")>B()'
        assert split_transform_chain(chain) == ['A("""x\ny""")', "B()"]

    def test_multiline_call_with_quoted_separator(self):
        chain = 'A(\n"x>y"\n)>B()'
        assert split_transform_chain(chain) == ['A(\n"x>y"\n)', "B()"]

    def test_whitespace_is_stripped(self):
        assert split_transform_chain("  A()  >  B()  ") == ["A()", "B()"]

    def test_unterminated_string_raises_value_error(self):
        with pytest.raises(ValueError, match="not valid Python syntax"):
            split_transform_chain('PrintData("unterminated)')

    def test_cli_chain_with_quoted_argument(self, tmp_path, monkeypatch):
        """A '>' inside a transform argument must not split the chain."""
        monkeypatch.chdir(tmp_path)
        stage_fixture(tmp_path, "inventory")
        main(["-i", "instance.xml", "-t", 'PrintData("a>b.xml")'])
        assert (tmp_path / "a>b.xml").exists()

    def test_cli_malformed_call_prints_clean_error(self, tmp_path, monkeypatch, capsys):
        """A transform that tokenizes but is not a valid literal call."""
        monkeypatch.chdir(tmp_path)
        stage_fixture(tmp_path, "inventory")
        with pytest.raises(SystemExit) as exitInfo:
            main(["-i", "instance.xml", "-t", "PrintData(**opts)"])
        assert exitInfo.value.code == 1
        captured = capsys.readouterr()
        assert "transform call" in captured.err
        assert "Traceback" not in captured.err

    def test_cli_malformed_chain_prints_clean_error(self, tmp_path, monkeypatch, capsys):
        """A chain that is not valid Python syntax at all."""
        monkeypatch.chdir(tmp_path)
        stage_fixture(tmp_path, "inventory")
        with pytest.raises(SystemExit) as exitInfo:
            main(["-i", "instance.xml", "-t", "PrintData("])
        assert exitInfo.value.code == 1
        captured = capsys.readouterr()
        assert "not valid Python syntax" in captured.err
        assert "Traceback" not in captured.err


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
        assert parser.getXmlOutputFileName().name == "instanceParsed.xml"

    def test_transforms_output_name(self, tmp_path):
        parser = run_parser("inventory")
        assert parser.getTransformsFileName().name == "instanceTransformed.xml"

    def test_transforms_output_name_without_file_input(self):
        parser = run_parser("inventory")
        parser.xmlFileInputName = None
        assert parser.getTransformsFileName().name == "output.xml"

    def test_boolean_parsed_output_uses_default_name(self, tmp_path):
        """``xmlFileOutput=True`` means 'use the default parsed name'."""
        import shutil

        source = fixture_dir("inventory")
        for name in ("instance.xml", "schema.xsd"):
            shutil.copy(source / name, tmp_path / name)
        PyXSD(
            str(tmp_path / "instance.xml"),
            str(tmp_path / "schema.xsd"),
            xmlFileOutput=True,
            transformOutputName=None,
        )
        assert (tmp_path / "instanceParsed.xml").is_file()


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
