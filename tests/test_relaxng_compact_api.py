"""End-to-end compilation of compact-syntax schemas through the public API.

These tests pin the public ``syntax`` selector, root auto-detection, the
argument validation that must happen before any input is read, and the
frontend boundary: a compact root is read natively and a trusted resource
policy's XML parser is never used for compact bytes.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.resource_policy import ResourceData
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

NS = "http://relaxng.org/ns/structure/1.0"

COMPACT = "element root { text }"
XML_SCHEMA = f'<element xmlns="{NS}" name="root"><empty/></element>'


class CountingStream(io.StringIO):
    """A readable stream that records whether compilation consumed it."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.reads = 0

    def read(self, *args: object) -> str:
        self.reads += 1
        return super().read(*args)  # type: ignore[arg-type]


class CompactPolicy:
    """A trusted policy that admits bytes and rejects XML parsing."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads: list[tuple[str, str | None, str]] = []
        self.parse_calls: list[ResourceData] = []

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        return ResourceData(source, self.payload)

    def parse(self, data: ResourceData, context: object) -> object:
        del context
        self.parse_calls.append(data)
        raise AssertionError("compact bytes must never reach the XML parser")


class XmlAcceptingPolicy(CompactPolicy):
    """A policy that records and performs the ordinary XML parse."""

    def parse(self, data: ResourceData, context: object) -> object:
        from pyxsd.namespaces import parse_with_namespaces

        self.parse_calls.append(data)
        return parse_with_namespaces(io.BytesIO(data.content), context)  # type: ignore[arg-type]


def assert_valid_root(schema: RelaxNGSchema) -> None:
    assert isinstance(schema, RelaxNGSchema)
    assert not schema.report.has_errors
    assert schema.parse(io.StringIO("<root>ok</root>"), typed=False).is_valid


def test_explicit_compact_compilation_validates_and_rejects_unexpected_children():
    schema = RelaxNGSchema.compile(io.StringIO(COMPACT), syntax="compact")

    assert_valid_root(schema)
    assert not schema.parse(io.StringIO("<root><child/></root>"), typed=False).is_valid


def test_the_relaxng_compile_alias_accepts_the_same_syntax_selector():
    schema = relaxng.compile(io.StringIO(COMPACT), syntax="compact")

    assert_valid_root(schema)
    assert relaxng.compile.__func__ is RelaxNGSchema.compile.__func__


@pytest.mark.parametrize("name", ["schema.rnc", "SCHEMA.RNC", "Schema.RnC"])
def test_auto_selects_compact_from_a_case_insensitive_named_path(tmp_path, name):
    path = tmp_path / name
    path.write_text(COMPACT, encoding="utf-8")

    schema = RelaxNGSchema.compile(path)

    assert_valid_root(schema)


def test_auto_selects_compact_from_a_named_stream(tmp_path):
    path = tmp_path / "schema.rnc"
    path.write_text(COMPACT, encoding="utf-8")

    with path.open(encoding="utf-8") as stream:
        schema = RelaxNGSchema.compile(stream)

    assert_valid_root(schema)


def test_auto_uses_only_the_url_path_component_for_remote_roots():
    compact_policy = CompactPolicy(COMPACT.encode("utf-8"))
    schema = RelaxNGSchema.compile(
        "https://example.test/schema.RNC?next=document.xml#body",
        resource_policy=compact_policy,
    )

    assert not schema.report.has_errors
    assert compact_policy.parse_calls == []

    xml_policy = XmlAcceptingPolicy(XML_SCHEMA.encode("utf-8"))
    xml_schema = RelaxNGSchema.compile(
        "https://example.test/schema.xml?next=schema.rnc",
        resource_policy=xml_policy,
    )
    assert not xml_schema.report.has_errors
    assert len(xml_policy.parse_calls) == 1


def test_auto_keeps_anonymous_and_other_named_sources_on_xml(tmp_path):
    from pyxsd.exceptions import PyXSDError

    xml_stream = io.StringIO(XML_SCHEMA)
    assert not RelaxNGSchema.compile(xml_stream).report.has_errors

    with pytest.raises(PyXSDError, match="well-formed XML"):
        RelaxNGSchema.compile(io.StringIO(COMPACT))

    named = tmp_path / "schema.txt"
    named.write_text(COMPACT, encoding="utf-8")
    with named.open(encoding="utf-8") as stream, pytest.raises(PyXSDError, match="well-formed XML"):
        RelaxNGSchema.compile(stream)


def test_explicit_xml_overrides_a_compact_path_suffix(tmp_path):
    path = tmp_path / "schema.rnc"
    path.write_text(XML_SCHEMA, encoding="utf-8")

    schema = RelaxNGSchema.compile(path, syntax="xml")

    assert not schema.report.has_errors
    assert schema.parse(io.StringIO("<root/>"), typed=False).is_valid


def test_explicit_compact_overrides_a_non_compact_path_suffix(tmp_path):
    path = tmp_path / "schema.rng"
    path.write_text(COMPACT, encoding="utf-8")

    schema = RelaxNGSchema.compile(path, syntax="compact")

    assert_valid_root(schema)


@pytest.mark.parametrize("syntax", ["rng", "", None, 1])
def test_an_invalid_syntax_value_fails_before_reading(syntax):
    stream = CountingStream(COMPACT)

    with pytest.raises(ValueError, match="syntax"):
        RelaxNGSchema.compile(stream, syntax=syntax)  # type: ignore[arg-type]

    assert stream.reads == 0


@pytest.mark.parametrize("option", ["xml_policy", "xml_limits"])
def test_compact_rejects_xml_parser_options_before_reading(option):
    stream = CountingStream(COMPACT)
    extra = (
        {"xml_policy": XmlParserPolicy.default()}
        if option == "xml_policy"
        else {"xml_limits": XmlLimits(max_bytes=1)}
    )

    with pytest.raises(ValueError, match="compact"):
        RelaxNGSchema.compile(stream, syntax="compact", **extra)

    assert stream.reads == 0


def test_auto_detection_precedes_reading_when_compact_rejects_xml_options(tmp_path):
    with pytest.raises(ValueError, match="compact"):
        RelaxNGSchema.compile(tmp_path / "missing.rnc", xml_limits=XmlLimits(max_bytes=1))


@pytest.mark.parametrize("malformed", ["element root {", "start = element {"])
def test_malformed_compact_roots_raise_the_compact_syntax_error(malformed):
    with pytest.raises(RelaxNGCompactSyntaxError):
        RelaxNGSchema.compile(io.StringIO(malformed), syntax="compact")


def test_malformed_autodetected_compact_roots_raise_the_compact_syntax_error(tmp_path):
    path = tmp_path / "broken.rnc"
    path.write_text("start = element {", encoding="utf-8")

    with pytest.raises(RelaxNGCompactSyntaxError):
        RelaxNGSchema.compile(path)


def test_a_trusted_policy_admits_compact_bytes_without_its_xml_parser(tmp_path):
    policy = CompactPolicy(COMPACT.encode("utf-8"))

    schema = RelaxNGSchema.compile(str(tmp_path / "remote.rnc"), resource_policy=policy)

    assert not schema.report.has_errors
    assert policy.parse_calls == []
    assert [call[2] for call in policy.reads] == ["schema"]


class _RecordingPolicy(CompactPolicy):
    """A policy that also records compilation-scope entry."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.events: list[tuple[str, bool]] = []

    @contextmanager
    def compilation(self, *, root_read: bool = True) -> Iterator[None]:
        self.events.append(("enter", root_read))
        try:
            yield
        finally:
            self.events.append(("exit",))

    _pyxsd_compilation_scope = compilation


def test_compact_compilation_enters_the_policy_scope_for_a_named_root(tmp_path):
    policy = _RecordingPolicy(COMPACT.encode("utf-8"))

    RelaxNGSchema.compile(str(tmp_path / "remote.rnc"), resource_policy=policy)

    assert policy.events == [("enter", True), ("exit",)]
