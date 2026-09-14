"""Schema-context isolation tests.

Two live PyXSD parsers must not corrupt each other's schema context:
the namespace overrides and form defaults are a per-parser snapshot on a
thread-local context, re-parsing a tree activates that parser's own
context, and failed or nested constructions restore the enclosing
context.
"""

import threading

import pytest

import pyxsd.schema_context as schema_context
from pyxsd.binding import ParseModes
from pyxsd.element_representatives.element_representative import (
    get_active_namespace_overrides,
    set_active_namespace_overrides,
)
from pyxsd.exceptions import PyXSDError
from pyxsd.parser import PyXSD

SIMPLE_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="b" type="xs:string"/></xs:schema>'
)
SIMPLE_INSTANCE = "<b>hello</b>"

MAIN_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="urn:main" xmlns:imp="urn:imp" elementFormDefault="qualified">'
    '  <xs:import namespace="urn:imp" schemaLocation="imp.xsd"/>'
    '  <xs:element name="r" type="imp:T"/>'
    "</xs:schema>"
)
IMP_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="urn:imp" elementFormDefault="unqualified">'
    '  <xs:complexType name="T"><xs:sequence>'
    '    <xs:element name="v" type="xs:int"/>'
    "  </xs:sequence></xs:complexType>"
    "</xs:schema>"
)
MAIN_INSTANCE = '<h:r xmlns:h="urn:main"><v>7</v></h:r>'


def _write_parser_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "main.xsd").write_text(MAIN_SCHEMA)
    (directory / "imp.xsd").write_text(IMP_SCHEMA)
    (directory / "instance.xml").write_text(MAIN_INSTANCE)
    return directory


def _write_simple_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bs.xsd").write_text(SIMPLE_SCHEMA)
    (directory / "bi.xml").write_text(SIMPLE_INSTANCE)
    return directory


def _parse(directory):
    return PyXSD(
        directory / "instance.xml",
        xsdFile=directory / "main.xsd",
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


def _parse_simple(directory):
    return PyXSD(
        directory / "bi.xml",
        xsdFile=directory / "bs.xsd",
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


def _type_namespace(parser, name):
    for entries in parser.components.values():
        for entry in entries:
            if entry.name == name:
                return entry.getNamespace()
    return "<missing>"


def test_override_install_is_a_snapshot():
    """Installing overrides copies them; later installs rebind."""
    overrides = {1: "urn:a"}
    set_active_namespace_overrides(overrides)
    installed = get_active_namespace_overrides()
    assert installed == {1: "urn:a"}

    # Mutating the caller's dict afterwards must not leak into the
    # installed map.
    overrides[2] = "urn:b"
    assert get_active_namespace_overrides() == {1: "urn:a"}

    # Installing again rebinds to a new map instead of clearing the
    # previous one in place; the earlier copy is untouched.
    set_active_namespace_overrides({3: "urn:c"})
    assert get_active_namespace_overrides() == {3: "urn:c"}
    assert installed == {1: "urn:a"}


def test_later_parser_cannot_corrupt_earlier_component_namespaces(tmp_path):
    """A second parser's schema run must not change the first parser's
    spliced components' namespaces."""
    first = _parse(_write_parser_files(tmp_path / "a"))
    before = _type_namespace(first, "T")
    assert before == "urn:imp"

    _parse_simple(_write_simple_files(tmp_path / "b"))

    assert _type_namespace(first, "T") == before


def test_concurrent_parsers_capture_their_own_context(tmp_path, monkeypatch):
    """Interleaved construction on two threads keeps each context.

    Both parsers are held at the snapshot point inside ``Schema``
    construction, so whichever installs second cannot rewrite the other
    parser's namespace overrides or form defaults.
    """
    import pyxsd.element_representatives.schema as schemamod

    namespace_barrier = threading.Barrier(2, timeout=10)
    form_barrier = threading.Barrier(2, timeout=10)
    original_namespace = schemamod.get_active_namespace_overrides
    original_forms = schemamod.get_active_form_defaults

    def gated_namespace():
        namespace_barrier.wait()
        return original_namespace()

    def gated_forms():
        form_barrier.wait()
        return original_forms()

    monkeypatch.setattr(schemamod, "get_active_namespace_overrides", gated_namespace)
    monkeypatch.setattr(schemamod, "get_active_form_defaults", gated_forms)

    a_dir = _write_parser_files(tmp_path / "a")
    b_dir = _write_simple_files(tmp_path / "b")
    results: dict[str, object] = {}

    def build(key, action):
        try:
            results[key] = action()
        except BaseException as exc:  # pragma: no cover - surfaced below
            results[key] = exc

    threads = [
        threading.Thread(target=build, args=("a", lambda: _parse(a_dir))),
        threading.Thread(target=build, args=("b", lambda: _parse_simple(b_dir))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    for key, value in results.items():
        if isinstance(value, BaseException):
            raise AssertionError(f"parser {key} failed under interleaving") from value
    first = results["a"]
    assert isinstance(first, PyXSD)
    assert _type_namespace(first, "T") == "urn:imp"


def test_failed_construction_leaves_no_active_context(tmp_path):
    """A parser that raises while building does not leak its context."""
    missing = tmp_path / "missing"
    with pytest.raises(PyXSDError):
        PyXSD(
            missing / "instance.xml",
            xsdFile=missing / "schema.xsd",
            xmlFileOutput="_No_Output_",
            transformOutputName="_No_Output_",
        )
    assert schema_context.current_context() is None

    # A later parser still builds normally.
    parser = _parse_simple(_write_simple_files(tmp_path / "b"))
    assert parser.report is not None


def test_nested_contexts_restore_the_outer_one():
    """Nested activations pop back to the enclosing context."""
    outer = schema_context.SchemaContext()
    inner = schema_context.SchemaContext()
    with schema_context.active_context(outer):
        assert schema_context.current_context() is outer
        with schema_context.active_context(inner):
            assert schema_context.current_context() is inner
        assert schema_context.current_context() is outer
    assert schema_context.current_context() is None


def test_parsexml_runs_under_the_parser_context(tmp_path, monkeypatch):
    """Re-parsing an earlier parser activates its own context."""
    first = _parse(_write_parser_files(tmp_path / "a"))
    _parse_simple(_write_simple_files(tmp_path / "b"))

    seen = []
    original = schema_context.active_context

    def spy(context):
        seen.append(context)
        return original(context)

    monkeypatch.setattr(schema_context, "active_context", spy)
    first.parseXML()

    assert first.schemaContext in seen
