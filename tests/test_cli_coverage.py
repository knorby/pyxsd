"""Coverage tests for the CLI's error, usage and transform-resolution paths.

Targets the branches the end-to-end suite in ``tests/test_cli.py`` leaves
unexercised: explicit ``--emit-stubs`` handling, stdin without a parsed
output name, logging-level selection, and the transform-module loaders'
file/spec fallbacks.
"""

import importlib
import logging
import sys
from pathlib import Path

import pytest

from pyxsd.cli import (
    _configure_logging,
    _load_module_from_file,
    _load_transform_module_by_normalized_name,
    main,
    resolve_transform_class,
)

INVENTORY_SCHEMA = Path(__file__).parent / "fixtures" / "inventory" / "schema.xsd"


class _FakeStdin:
    """A non-tty stdin stand-in (mirrors ``tests/test_cli.py``)."""

    def __init__(self, text):
        self._text = text

    def isatty(self):
        return False

    def read(self):
        return self._text


# ---------------------------------------------------------------------------
# Transform-module loaders
# ---------------------------------------------------------------------------


def test_load_module_from_file_returns_none_for_an_unloadable_spec(tmp_path):
    """A path with no recognized source suffix yields no module."""
    source = tmp_path / "probe.txt"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    assert _load_module_from_file("Probe", source) is None


def test_load_module_from_file_keeps_an_existing_sys_path_entry(tmp_path, monkeypatch):
    """A directory already on ``sys.path`` is not added (nor later removed)."""
    source = tmp_path / "path_probe.py"
    source.write_text("VALUE = 7\n", encoding="utf-8")
    directory = str(tmp_path)
    monkeypatch.setattr(sys, "path", [directory, *sys.path])

    module = _load_module_from_file("path_probe", source)

    assert module is not None
    assert module.VALUE == 7
    assert directory in sys.path


def test_normalized_module_lookup_finds_the_shipped_transform():
    """The underscore-insensitive fallback resolves a shipped transform."""
    module = _load_transform_module_by_normalized_name("PrintData")
    assert module is importlib.import_module("pyxsd.transforms.print_data")
    assert module.PrintData is not None


def test_resolve_transform_class_accepts_a_string_search_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_transform_class("PrintData", search_paths=str(tmp_path)).__name__ == "PrintData"


def test_resolve_transform_class_accepts_a_path_search_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_transform_class("PrintData", search_paths=tmp_path).__name__ == "PrintData"


# ---------------------------------------------------------------------------
# XML input errors
# ---------------------------------------------------------------------------


def test_missing_xml_input_is_reported_as_a_clean_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", str(tmp_path / "nope.xml"), "-s", str(INVENTORY_SCHEMA), "-o", "/dev/null"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "the xml input could not be read" in err
    assert "Traceback" not in err


def test_malformed_xml_input_is_reported_as_a_clean_error(tmp_path, capsys):
    instance = tmp_path / "bad.xml"
    instance.write_text("<root><unclosed></root>", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["-i", str(instance), "-s", str(INVENTORY_SCHEMA), "-o", "/dev/null"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "not well-formed XML" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# --emit-stubs / --stubs-compact
# ---------------------------------------------------------------------------


def test_stubs_compact_without_emit_stubs_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--stubs-compact"])
    assert excinfo.value.code == 2
    assert "--stubs-compact requires --emit-stubs" in capsys.readouterr().err


def test_emit_stubs_without_a_schema_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--emit-stubs"])
    assert excinfo.value.code == 2
    assert "--emit-stubs requires a schema file" in capsys.readouterr().err


def test_emit_stubs_writes_the_stub_to_stdout(capsys):
    main(["--emit-stubs", "-s", str(INVENTORY_SCHEMA)])
    assert "class itemType" in capsys.readouterr().out


def test_emit_stubs_writes_the_stub_to_a_file(tmp_path):
    output = tmp_path / "inventory.pyi"
    main(["--emit-stubs", str(output), "-s", str(INVENTORY_SCHEMA)])
    assert "class itemType" in output.read_text(encoding="utf-8")


def test_emit_stubs_reports_a_schema_load_failure(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--emit-stubs", "-s", str(tmp_path / "missing.xsd")])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "pyxsd: error:" in err
    assert "Traceback" not in err


def test_emit_stubs_compact_omits_the_import_preamble(capsys):
    main(["--emit-stubs", "--stubs-compact", "-s", str(INVENTORY_SCHEMA)])
    out = capsys.readouterr().out
    assert "class itemType" in out
    assert "from pyxsd" not in out


# ---------------------------------------------------------------------------
# stdin parsing
# ---------------------------------------------------------------------------


def test_stdin_parsed_output_without_a_filename_writes_nothing(tmp_path, monkeypatch):
    """``-k`` over stdin has no input filename, so no parsed file is written."""
    monkeypatch.chdir(tmp_path)
    instance = (
        '<inventory xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="schema.xsd">'
        '<item id="a-1"><name>n</name><price>1</price><quantity>2</quantity></item>'
        "</inventory>"
    )
    monkeypatch.setattr(sys, "stdin", _FakeStdin(instance))

    main(["-k", "-s", str(INVENTORY_SCHEMA)])

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def test_configure_logging_selects_the_requested_level():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers.clear()
        _configure_logging(verbose=True, quiet=False)
        assert root.level == logging.DEBUG

        root.handlers.clear()
        _configure_logging(verbose=False, quiet=True)
        assert root.level == logging.CRITICAL

        root.handlers.clear()
        _configure_logging(verbose=False, quiet=False)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
