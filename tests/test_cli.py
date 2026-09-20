"""End-to-end tests for the command line interface and its helpers."""

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conftest import assert_xml_canonically_equal, fixture_dir
from pyxsd.cli import (
    _transform_module_names as _transformModuleNames,
)
from pyxsd.cli import (
    main,
    split_transform_chain,
)
from pyxsd.cli import (
    parse_transform_call as parseTransformCall,
)
from pyxsd.schema_hints import schema_location_info
from pyxsd.transforms import Transform

EXAMPLES_TRANSFORMS = Path(__file__).parent.parent / "examples" / "legacy"


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


def test_stream_input_default_transform_output(tmp_path, monkeypatch):
    """``-d`` with stream input writes output.xml to the working directory."""
    monkeypatch.chdir(tmp_path)
    xml = fixture_dir("inventory").joinpath("instance.xml").read_text()
    monkeypatch.setattr("sys.stdin", _FakeStdin(xml))
    main(["-s", str(fixture_dir("inventory") / "schema.xsd"), "-t", "PrintData()", "-d"])
    assert (tmp_path / "output.xml").exists()


def test_pass_through_final_transform_skips_output(tmp_path, monkeypatch):
    """A last transform that does not return a tree skips the output write."""
    monkeypatch.chdir(tmp_path)
    stage_fixture(tmp_path, "inventory")
    (tmp_path / "noop_probe.py").write_text("def NoopProbe(root):\n    return 'plain'\n")
    output = tmp_path / "out.xml"
    main(["-i", "instance.xml", "-t", "NoopProbe()", "-o", str(output)])
    assert not output.exists()


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

    def test_invalid_character_raises_value_error(self):
        """Tokenizer garbage must fail the split on every supported version.

        Python 3.11's tokenizer emits ERRORTOKEN for characters it cannot
        recognize (including an unterminated string outside any call
        parentheses) without raising, while 3.12+'s C tokenizer raises
        TokenError. Either way the chain is not valid Python syntax.
        """
        with pytest.raises(ValueError, match="not valid Python syntax"):
            split_transform_chain('A() > "unterminated')

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


class TestResolveTransformClass:
    def test_resolves_via_explicit_search_path(self, tmp_path, monkeypatch):
        """An explicit search path is consulted when cwd has no match."""
        monkeypatch.chdir(tmp_path)
        from pyxsd.cli import resolve_transform_class

        transformCls = resolve_transform_class("ExpandCell", search_paths=[EXAMPLES_TRANSFORMS])
        assert transformCls.__name__ == "ExpandCell"
        assert issubclass(transformCls, Transform)

    def test_cwd_is_checked_before_search_paths(self, tmp_path, monkeypatch):
        """The working directory wins over a later search path."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bogus.py").write_text("class Bogus:\n    here = 'cwd'\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "bogus.py").write_text("class Bogus:\n    here = 'search'\n")
        from pyxsd.cli import resolve_transform_class

        assert resolve_transform_class("Bogus", search_paths=[elsewhere]).here == "cwd"

    def test_module_without_the_class_raises_pyxsd_error(self, tmp_path, monkeypatch):
        """A module that imports but lacks the class is a clean PyXSDError."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "bogus.py").write_text("OTHER = 1\n")
        from pyxsd.cli import resolve_transform_class
        from pyxsd.exceptions import PyXSDError

        with pytest.raises(PyXSDError, match="does not define that class"):
            resolve_transform_class("Bogus")

    def test_unknown_transform_still_raises_import_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from pyxsd.cli import resolve_transform_class

        with pytest.raises(ImportError, match="NoSuchTransform"):
            resolve_transform_class("NoSuchTransform")


class TestMaterialize:
    def test_transform_class_is_wrapped(self, tmp_path, monkeypatch):
        """A Transform subclass becomes a root-first callable."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dummy_probe.py").write_text(
            "from pyxsd.transforms.transform import Transform\n"
            "\n"
            "\n"
            "class DummyProbe(Transform):\n"
            "    def __init__(self, root):\n"
            "        super().__init__(root)\n"
            "\n"
            "    def __call__(self, multiplier=1):\n"
            "        self.root.seen = multiplier\n"
            "        return self.root\n"
        )
        from pyxsd.cli import _materialize

        fn, args, kwargs = _materialize("DummyProbe(multiplier=3)")
        assert args == []
        assert kwargs == {"multiplier": 3}

        class Root:
            pass

        root = Root()
        assert fn(root, *args, **kwargs) is root
        assert root.seen == 3

    def test_plain_callable_is_used_directly(self, tmp_path, monkeypatch):
        """A non-Transform callable receives the root as its first argument."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "fn_probe.py").write_text(
            "def FnProbe(root, tag=None):\n    root.tagged = tag\n    return 'plain'\n"
        )
        from pyxsd.cli import _materialize

        fn, args, kwargs = _materialize("FnProbe(tag='x')")
        assert args == []
        assert kwargs == {"tag": "x"}

        class Root:
            pass

        root = Root()
        assert fn(root, *args, **kwargs) == "plain"
        assert root.tagged == "x"

    def test_signature_mismatch_raises_pyxsd_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dummy_probe.py").write_text(
            "from pyxsd.transforms.transform import Transform\n"
            "\n"
            "\n"
            "class DummyProbe(Transform):\n"
            "    def __init__(self, root):\n"
            "        super().__init__(root)\n"
            "\n"
            "    def __call__(self, multiplier=1):\n"
            "        return self.root\n"
        )
        from pyxsd.cli import _materialize
        from pyxsd.exceptions import PyXSDError

        with pytest.raises(PyXSDError, match="does not match the signature"):
            _materialize("DummyProbe(bogus=1)")


class TestTransformModuleNames:
    def test_single_word(self):
        assert _transformModuleNames("PrintData") == ["printData", "print_data"]

    def test_multi_word(self):
        # The generic camel->snake conversion splits acronym runs, so
        # 'ParseHTMLTree' becomes 'parse_html_tree'; module resolution
        # compensates with an underscore-insensitive fallback (see
        # test_transform_module_load_acronym_fallback).
        assert _transformModuleNames("ParseHTMLTree") == [
            "parseHTMLTree",
            "parse_html_tree",
        ]

    def test_no_case_conversion_collapses_to_one(self):
        assert _transformModuleNames("Foo") == ["foo"]

    def test_transform_module_load_acronym_fallback(self, tmp_path, monkeypatch):
        """Acronym-split names still resolve to a matching module file.

        Neither exact candidate (``parseHTMLTree`` /
        ``parse_html_tree``) matches a file named
        ``parse_htmltree.py``; the underscore-insensitive fallback
        must pick it up.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "parse_htmltree.py").write_text("class ParseHTMLTree:\n    pass\n")
        from pyxsd.cli import resolve_transform_class

        assert resolve_transform_class("ParseHTMLTree").__module__ == "parse_htmltree"


def _unparsed_root(xml_text, tmp_path):
    """The plain ElementTree root of *xml_text*.

    The schema-hint helpers are pure functions of the parsed root
    element, so the tests inspect them on an untouched tree.
    """
    source = tmp_path / "probe.xml"
    source.write_text(xml_text)
    return ET.parse(source).getroot()


class TestSchemaInfo:
    def test_no_namespace_schema_location(self, tmp_path):
        root = _unparsed_root(
            '<inventory xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:noNamespaceSchemaLocation="schema.xsd"/>',
            tmp_path,
        )
        assert schema_location_info(root, "l") == "schema.xsd"
        assert schema_location_info(root, "n") is None
        assert schema_location_info(root, "t").endswith("noNamespaceSchemaLocation")

    def test_namespace_schema_location(self, tmp_path):
        root = _unparsed_root(
            '<inventory xmlns="http://example.com/ns"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:schemaLocation="http://example.com/ns schema.xsd"/>',
            tmp_path,
        )
        assert schema_location_info(root, "l") == "schema.xsd"
        assert schema_location_info(root, "n") == "http://example.com/ns"
        assert schema_location_info(root, "t").endswith("schemaLocation")

    def test_no_hints_returns_none(self, tmp_path):
        root = _unparsed_root("<inventory/>", tmp_path)
        assert schema_location_info(root, "l") is None
        assert schema_location_info(root, "n") is None
