"""Lazy ENTITY/ENTITIES declaration validation through ``Schema.parse``.

A compiled schema that can validate ``xs:ENTITY``/``xs:ENTITIES`` values
gets its instance document's DTD prolog collected and the values checked
against the declared unparsed entities. Schemas that cannot, never pay
for collection; pre-parsed elements that pyxsd did not parse, custom
resource policies, and non-seekable top-level discovery streams keep the
lexical-only behaviour.
"""

from __future__ import annotations

import gc
import io
import weakref
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

import pyxsd.schema
from pyxsd import Document, ParseModes, Schema, parse
from pyxsd.dtd_declarations import DocumentDeclarations, collect_document_declarations
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.resource_policy import ResourceData
from pyxsd.xsd_data_types import ENTITIES, ENTITY
from test_threading import run_threaded

XSD_NS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

_ENTITY_ROOT_XSD = (
    f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
    '  <xs:element name="r" type="xs:ENTITY"/>\n</xs:schema>\n'
)


def doctype(name: str, entities: tuple[str, ...] = ()) -> str:
    """An internal subset declaring *entities* as unparsed entities."""
    declarations = "".join(
        f'<!NOTATION n SYSTEM "n"><!ENTITY {entity} SYSTEM "{entity}.gif" NDATA n>'
        for entity in entities
    )
    return f"<!DOCTYPE {name} [{declarations}]>"


def write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def compile_entity_schema(tmp_path: Path, type_declaration: str, root_type: str) -> Schema:
    schema_text = (
        f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
        f'  <xs:element name="r" type="{root_type}"/>\n'
        f"{type_declaration}\n</xs:schema>\n"
    )
    return Schema.compile(write(tmp_path, "schema.xsd", schema_text), mode=ParseModes.NAMESPACED)


def parse_root_value(
    schema: Schema, tmp_path: Path, value: str, entities: tuple[str, ...], name: str
) -> Any:
    instance = f"{doctype('r', entities)}<r>{value}</r>"
    return schema.parse(write(tmp_path, name, instance))


# ---------------------------------------------------------------------------
# Step 1: validation behaviour
# ---------------------------------------------------------------------------


def test_entity_value_must_be_declared(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")

    declared = parse_root_value(schema, tmp_path, "alpha", ("alpha",), "ok.xml")
    assert not declared.report.has_errors
    assert not declared.report.warnings

    undeclared = parse_root_value(schema, tmp_path, "omega", ("alpha",), "bad.xml")
    assert undeclared.report.has_errors


def test_entities_list_rejects_any_undeclared_token(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITIES")

    declared = parse_root_value(schema, tmp_path, "alpha beta", ("alpha", "beta"), "ok.xml")
    assert not declared.report.has_errors

    undeclared = parse_root_value(schema, tmp_path, "alpha omega", ("alpha", "beta"), "bad.xml")
    assert undeclared.report.has_errors


def test_restriction_of_entity_enforces_declarations(tmp_path: Path) -> None:
    restriction = (
        '<xs:simpleType name="restricted-entity"><xs:restriction base="xs:ENTITY"/></xs:simpleType>'
    )
    schema = compile_entity_schema(tmp_path, restriction, "restricted-entity")

    declared = parse_root_value(schema, tmp_path, "alpha", ("alpha",), "ok.xml")
    assert not declared.report.has_errors

    undeclared = parse_root_value(schema, tmp_path, "omega", ("alpha",), "bad.xml")
    assert undeclared.report.has_errors


def test_schema_list_of_entity_enforces_declarations(tmp_path: Path) -> None:
    list_type = '<xs:simpleType name="entity-list"><xs:list itemType="xs:ENTITY"/></xs:simpleType>'
    schema = compile_entity_schema(tmp_path, list_type, "entity-list")

    declared = parse_root_value(schema, tmp_path, "alpha beta", ("alpha", "beta"), "ok.xml")
    assert not declared.report.has_errors

    undeclared = parse_root_value(schema, tmp_path, "alpha omega", ("alpha",), "bad.xml")
    assert undeclared.report.has_errors


def test_union_member_entity_enforces_declarations(tmp_path: Path) -> None:
    union = (
        '<xs:simpleType name="entity-or-integer">'
        '<xs:union memberTypes="xs:ENTITY xs:integer"/></xs:simpleType>'
    )
    schema = compile_entity_schema(tmp_path, union, "entity-or-integer")

    declared = parse_root_value(schema, tmp_path, "alpha", ("alpha",), "ok1.xml")
    assert not declared.report.has_errors

    integer = parse_root_value(schema, tmp_path, "-1", ("alpha",), "ok2.xml")
    assert not integer.report.has_errors

    undeclared = parse_root_value(schema, tmp_path, "omega", ("alpha",), "bad.xml")
    assert undeclared.report.has_errors


def test_missing_doctype_rejects_every_entity_value(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")

    document = schema.parse(write(tmp_path, "none.xml", "<r>alpha</r>"))
    assert document.report.has_errors


ATTRIBUTE_DEFAULT_XSD = (
    f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
    '  <xs:element name="doc"><xs:complexType><xs:sequence>\n'
    '    <xs:element ref="para" maxOccurs="unbounded"/>\n'
    "  </xs:sequence></xs:complexType></xs:element>\n"
    '  <xs:element name="para"><xs:complexType><xs:simpleContent>\n'
    '    <xs:extension base="xs:string">\n'
    '      <xs:attribute name="entity" default="alpha" type="xs:ENTITY"/>\n'
    "    </xs:extension></xs:simpleContent></xs:complexType></xs:element>\n"
    "</xs:schema>\n"
)


def test_attribute_default_must_be_declared(tmp_path: Path) -> None:
    schema = Schema.compile(
        write(tmp_path, "schema.xsd", ATTRIBUTE_DEFAULT_XSD), mode=ParseModes.NAMESPACED
    )

    declared = schema.parse(
        write(tmp_path, "ok.xml", f"{doctype('doc', ('alpha',))}<doc><para/></doc>")
    )
    assert not declared.report.has_errors

    undeclared = schema.parse(
        write(tmp_path, "bad.xml", f"{doctype('doc', ('beta',))}<doc><para/></doc>")
    )
    assert undeclared.report.has_errors


ELEMENT_DEFAULT_XSD = (
    f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
    '  <xs:element name="doc"><xs:complexType><xs:sequence>\n'
    '    <xs:element name="e" type="xs:ENTITY" default="alpha"/>\n'
    "  </xs:sequence></xs:complexType></xs:element>\n"
    "</xs:schema>\n"
)


def test_element_default_must_be_declared(tmp_path: Path) -> None:
    schema = Schema.compile(
        write(tmp_path, "schema.xsd", ELEMENT_DEFAULT_XSD), mode=ParseModes.NAMESPACED
    )

    declared = schema.parse(
        write(tmp_path, "ok.xml", f"{doctype('doc', ('alpha',))}<doc><e/></doc>")
    )
    assert not declared.report.has_errors

    undeclared = schema.parse(
        write(tmp_path, "bad.xml", f"{doctype('doc', ('beta',))}<doc><e/></doc>")
    )
    assert undeclared.report.has_errors


def test_external_subset_keeps_lexical_validation_with_one_warning(
    tmp_path: Path,
) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")

    document = schema.parse(
        write(
            tmp_path,
            "external.xml",
            '<!DOCTYPE r SYSTEM "external.dtd"><r>omega</r>',
        )
    )
    assert not document.report.has_errors
    warnings = [issue.code for issue in document.report.warnings]
    assert warnings.count("dtd-external-skipped") == 1


def test_external_subset_warning_is_per_document_not_per_value(
    tmp_path: Path,
) -> None:
    schema = Schema.compile(
        write(
            tmp_path,
            "schema.xsd",
            f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
            '  <xs:element name="doc"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="one" type="xs:ENTITY"/>\n'
            '    <xs:element name="two" type="xs:ENTITY"/>\n'
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>\n",
        ),
        mode=ParseModes.NAMESPACED,
    )

    document = schema.parse(
        write(
            tmp_path,
            "two-values.xml",
            '<!DOCTYPE doc SYSTEM "external.dtd"><doc><one>omega</one><two>sigma</two></doc>',
        )
    )
    assert not document.report.has_errors
    warnings = [issue.code for issue in document.report.warnings]
    assert warnings.count("dtd-external-skipped") == 1


def test_direct_datatype_construction_outside_a_parse_is_unchanged() -> None:
    assert str(ENTITY("undeclared")) == "undeclared"
    assert str(ENTITIES("undeclared names")) == "undeclared names"


# ---------------------------------------------------------------------------
# Step 1: lazy activation
# ---------------------------------------------------------------------------


class CollectionSpy:
    """Wraps ``pyxsd.schema.collect_document_declarations`` and counts calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self.real = collect_document_declarations
        monkeypatch.setattr(pyxsd.schema, "collect_document_declarations", self)

    def __call__(self, source: Any, *, policy: Any) -> DocumentDeclarations | None:
        self.calls += 1
        return self.real(source, policy=policy)


STRING_ONLY_XSD = (
    f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
    '  <xs:element name="r"><xs:complexType><xs:sequence>\n'
    '    <xs:element name="note" type="xs:string"/>\n'
    "  </xs:sequence></xs:complexType></xs:element>\n"
    "</xs:schema>\n"
)


def test_string_only_schema_never_collects(tmp_path: Path, monkeypatch) -> None:
    spy = CollectionSpy(monkeypatch)
    schema = Schema.compile(
        write(tmp_path, "schema.xsd", STRING_ONLY_XSD), mode=ParseModes.NAMESPACED
    )
    instance = write(tmp_path, "instance.xml", "<r><note>hello</note></r>")

    schema.parse(instance)
    parse(instance, xsd=write(tmp_path, "schema2.xsd", STRING_ONLY_XSD))

    assert spy.calls == 0


def test_entity_schema_collects_once_per_parse(tmp_path: Path, monkeypatch) -> None:
    spy = CollectionSpy(monkeypatch)
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = write(tmp_path, "instance.xml", f"{doctype('r', ('alpha',))}<r>alpha</r>")

    schema.parse(instance)
    assert spy.calls == 1

    schema.parse(instance)
    assert spy.calls == 2

    parse(instance, xsd=write(tmp_path, "again.xsd", _ENTITY_ROOT_XSD))
    assert spy.calls == 3


def test_recursive_union_activation_terminates(tmp_path: Path, monkeypatch) -> None:
    from pyxsd.schema import (
        _reaches_unparsed_entity_types,
        _schema_validates_unparsed_entities,
    )

    first = type("First", (), {})
    second = type("Second", (), {})
    first._unionMembers = (second,)
    second._unionMembers = (first,)  # a cycle no XSD document could express

    visited: set[int] = set()
    assert _reaches_unparsed_entity_types(first, visited) is False
    assert id(first) in visited and id(second) in visited

    cycling_with_entity = type("Cycling", (), {})
    other = type("Other", (), {})
    cycling_with_entity._unionMembers = (ENTITY, other)
    other._unionMembers = (cycling_with_entity,)
    assert _reaches_unparsed_entity_types(cycling_with_entity, set()) is True

    spy = CollectionSpy(monkeypatch)
    schema = Schema.compile(
        write(tmp_path, "schema.xsd", STRING_ONLY_XSD), mode=ParseModes.NAMESPACED
    )
    schema.classes["recursive"] = first
    schema._entity_declarations_needed = None
    assert _schema_validates_unparsed_entities(schema) is False
    schema.parse(write(tmp_path, "instance.xml", "<r><note>text</note></r>"))
    assert spy.calls == 0


def test_activation_survives_descriptors_that_fail_to_resolve(
    tmp_path: Path,
) -> None:
    from pyxsd.element_representatives.element_representative import (
        ElementRepresentative,
    )
    from pyxsd.schema import _schema_validates_unparsed_entities

    class BrokenDescriptor(ElementRepresentative):
        def getType(self):
            raise TypeError("unresolved reference")

    schema = Schema.compile(
        write(tmp_path, "schema.xsd", STRING_ONLY_XSD), mode=ParseModes.NAMESPACED
    )
    broken = BrokenDescriptor.__new__(BrokenDescriptor)
    schema.classes["schema"].broken_declaration = broken
    schema._entity_declarations_needed = None

    assert _schema_validates_unparsed_entities(schema) is False
    document = schema.parse(write(tmp_path, "instance.xml", "<r><note>x</note></r>"))
    assert not document.report.has_errors


# ---------------------------------------------------------------------------
# Step 2: input boundaries and isolation
# ---------------------------------------------------------------------------


def test_arbitrary_preparsed_element_stays_lexical_only(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    tree = ET.fromstring(f"{doctype('r', ('alpha',))}<r>omega</r>")

    document = schema.parse(tree)

    assert not document.report.has_errors
    assert not document.report.warnings


class _MemoryPolicy:
    """A custom resource policy that parses from memory."""

    def __init__(self, documents: dict[str, str]) -> None:
        self._documents = documents

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        return ResourceData(source, self._documents[source].encode("utf-8"))

    def parse(self, data: ResourceData, context: NamespaceContext) -> ET.Element:
        return ET.fromstring(data.content)


def test_custom_resource_policy_stays_lexical_only(tmp_path: Path) -> None:
    instance = f"{doctype('r', ('alpha',))}<r>omega</r>"
    schema_path = write(tmp_path, "schema.xsd", _ENTITY_ROOT_XSD)
    document = parse(
        "memory:instance.xml",
        xsd=schema_path,
        resource_policy=_MemoryPolicy(
            {"memory:instance.xml": instance, schema_path: _ENTITY_ROOT_XSD}
        ),
    )
    assert not document.report.has_errors
    assert not document.report.warnings


def test_path_parsed_element_carries_provenance(tmp_path: Path) -> None:
    instance_path = write(tmp_path, "instance.xml", f"{doctype('r', ('alpha',))}<r>omega</r>")

    # The XSTS driver's shape: parse the instance first, compile with the
    # shared context, then bind the pre-parsed element.
    context = NamespaceContext()
    tree = parse_with_namespaces(instance_path, context)
    schema = Schema.compile(
        write(tmp_path, "schema.xsd", _ENTITY_ROOT_XSD),
        mode=ParseModes.NAMESPACED,
        namespace_context=context,
    )
    document = schema.parse(tree)

    assert document.report.has_errors


def test_seekable_top_level_discovery_collects(tmp_path: Path) -> None:
    schema_path = write(tmp_path, "schema.xsd", _ENTITY_ROOT_XSD)
    instance = f"{doctype('r', ('alpha',))}<r>omega</r>"

    document = parse(io.BytesIO(instance.encode("utf-8")), xsd=schema_path)

    assert document.report.has_errors


class NonSeekableBytes(io.BytesIO):
    def seekable(self) -> bool:
        return False


class BareReader:
    """A read-only stream without even a ``seekable`` attribute."""

    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


@pytest.mark.parametrize("stream_factory", [NonSeekableBytes, BareReader])
def test_nonseekable_schema_parse_stream_replays_body(tmp_path: Path, stream_factory: type) -> None:
    schema = Schema.compile(
        write(
            tmp_path,
            "schema.xsd",
            f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
            '  <xs:element name="doc"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="one" type="xs:ENTITY"/>\n'
            '    <xs:element name="note" type="xs:string"/>\n'
            '    <xs:element name="two" type="xs:ENTITY"/>\n'
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>\n",
        ),
        mode=ParseModes.NAMESPACED,
    )
    # ``one`` takes a declared name, ``two`` an undeclared one: the parse
    # must both bind the complete remainder and reject the last child.
    instance = (
        f"{doctype('doc', ('alpha',))}"
        "<doc><one>alpha</one><note>still bound</note><two>omega</two></doc>"
    )

    document = schema.parse(stream_factory(instance.encode("utf-8")))

    assert document.report.has_errors
    assert str(document.root.one) == "alpha"
    assert str(document.root.note) == "still bound"
    # ``two`` was reached through the replayed remainder and rejected;
    # an unbound remainder would instead report a content-model error.
    assert any("'two'" in issue.message for issue in document.report.errors)


def test_nonseekable_text_schema_parse_stream_replays_body(tmp_path: Path) -> None:
    class NonSeekableText(io.StringIO):
        def seekable(self) -> bool:
            return False

    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = f"{doctype('r', ('alpha',))}<r>alpha</r>"

    document = schema.parse(NonSeekableText(instance))

    assert not document.report.has_errors
    assert str(document.root) == "alpha"


def test_nonseekable_top_level_discovery_stays_lexical_only(tmp_path: Path, monkeypatch) -> None:
    spy = CollectionSpy(monkeypatch)
    schema_path = write(tmp_path, "schema.xsd", _ENTITY_ROOT_XSD)
    instance = f"{doctype('r', ('alpha',))}<r>omega</r>"

    document = parse(NonSeekableBytes(instance.encode("utf-8")), xsd=schema_path)

    assert not document.report.has_errors
    assert spy.calls == 0


def test_declaration_context_resets_after_binding_exception(tmp_path: Path, monkeypatch) -> None:
    from pyxsd.dtd_declarations import active_declaration_evidence

    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = write(tmp_path, "instance.xml", f"{doctype('r', ('alpha',))}<r>alpha</r>")

    def explode(schema: Any, tree: Any, report: Any) -> Any:
        assert active_declaration_evidence() is not None
        raise RuntimeError("binding failed")

    monkeypatch.setattr(pyxsd.schema, "bind_instance", explode)
    with pytest.raises(RuntimeError, match="binding failed"):
        schema.parse(instance)

    assert active_declaration_evidence() is None
    assert str(ENTITY("undeclared")) == "undeclared"


def test_concurrent_parses_keep_declarations_isolated(tmp_path: Path) -> None:
    schemas = [
        compile_entity_schema(tmp_path, "", "xs:ENTITY"),
        compile_entity_schema(tmp_path, "", "xs:ENTITY"),
    ]
    documents = [
        write(tmp_path, "a.xml", f"{doctype('r', ('alpha',))}<r>alpha</r>"),
        write(tmp_path, "b.xml", f"{doctype('r', ('beta',))}<r>beta</r>"),
    ]
    mismatches = [
        write(tmp_path, "a-cross.xml", f"{doctype('r', ('alpha',))}<r>beta</r>"),
        write(tmp_path, "b-cross.xml", f"{doctype('r', ('beta',))}<r>alpha</r>"),
    ]

    def worker(i: int, barrier: Any) -> None:
        barrier.wait()
        schema = schemas[i % 2]
        assert not schema.parse(documents[i % 2]).report.has_errors
        assert schema.parse(mismatches[i % 2]).report.has_errors

    run_threaded(worker, num_threads=2, pass_count=True)


def test_root_provenance_is_weak(tmp_path: Path) -> None:
    context = NamespaceContext()
    stream = io.BytesIO(b"<r/>")
    root = parse_with_namespaces(stream, context)

    assert isinstance(context.root_source(root), io.BytesIO)
    keeper = weakref.ref(root)
    del root
    gc.collect()

    assert keeper() is None


def test_stream_provenance_is_weak_and_dies_with_the_stream() -> None:
    context = NamespaceContext()
    stream = io.BytesIO(b"<r/>")
    root = parse_with_namespaces(stream, context)

    del stream
    gc.collect()

    assert context.root_source(root) is None


def test_unrewindable_stream_provenance_falls_back_to_none(tmp_path: Path) -> None:
    class SeekBreaks(io.BytesIO):
        def seek(self, *args: Any) -> int:
            raise OSError("cannot rewind")

    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = f"{doctype('r', ('alpha',))}<r>omega</r>"

    document = schema.parse(SeekBreaks(instance.encode("utf-8")))

    assert not document.report.has_errors
    assert not document.report.warnings


def test_failed_collection_after_a_successful_parse_is_not_fatal(
    tmp_path: Path, monkeypatch
) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = write(tmp_path, "instance.xml", f"{doctype('r', ('alpha',))}<r>alpha</r>")

    def unreachable(source: Any, *, policy: Any) -> DocumentDeclarations | None:
        raise OSError("source vanished")

    monkeypatch.setattr(pyxsd.schema, "collect_document_declarations", unreachable)
    document = schema.parse(instance)

    assert not document.report.has_errors
    assert not document.report.warnings


def test_unweakreffable_seekable_stream_records_no_provenance(
    tmp_path: Path,
) -> None:
    class SlottedStream:
        """A seekable stream class that cannot be weakly referenced."""

        __slots__ = ("_data",)

        def __init__(self, data: bytes) -> None:
            self._data = io.BytesIO(data)

        def read(self, size: int = -1) -> bytes:
            return self._data.read(size)

        def seekable(self) -> bool:
            return True

    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = f"{doctype('r', ('alpha',))}<r>omega</r>"

    document = schema.parse(SlottedStream(instance.encode("utf-8")))

    assert not document.report.has_errors
    assert not document.report.warnings


def test_truncated_nonseekable_prolog_is_reported_by_the_ordinary_parser(
    tmp_path: Path,
) -> None:
    from pyxsd.exceptions import PyXSDError
    from pyxsd.xml_policy import XmlLimits

    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")

    # The capped collector reads to EOF without crossing the budget, so
    # the empty-read arm of the adapter runs and collection still defers
    # the malformed-prolog diagnosis to the ordinary parser.
    with pytest.raises(PyXSDError, match="not well-formed"):
        schema.parse(
            NonSeekableBytes(b"<!DOCTYPE r [<!ENTITY a"),
            xml_limits=XmlLimits(max_bytes=128),
        )


def test_replay_reader_reconstructs_the_document_exactly() -> None:
    from pyxsd.schema import _PrologReplay

    document = b'<!DOCTYPE r [<!ENTITY a SYSTEM "a" NDATA n>]><r>body</r>'
    replay = _PrologReplay(io.BytesIO(document))
    # Collection phase: reads land in the buffer and the stream advances.
    assert replay.read(20) == document[:20]
    replay.finish_collection()

    # Replay phase: reads serve the buffered bytes first -- arbitrary
    # sizes, read(-1) spanning the buffer/stream boundary, then EOF.
    assert replay.read(5) == document[:5]
    assert replay.read(-1) == document[5:]
    assert replay.read(-1) == b""

    fresh = _PrologReplay(io.BytesIO(document))
    fresh.read(10)
    fresh.finish_collection()
    pieces = [fresh.read(3) for _ in range(len(document))]
    assert b"".join(piece for piece in pieces if piece) == document


def test_replay_reader_serves_tail_when_collection_stops_mid_document(
    tmp_path: Path, monkeypatch
) -> None:
    from pyxsd import dtd_declarations

    schema = Schema.compile(
        write(
            tmp_path,
            "schema.xsd",
            f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
            '  <xs:element name="doc"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="one" type="xs:ENTITY"/>\n'
            '    <xs:element name="note" type="xs:string"/>\n'
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>\n",
        ),
        mode=ParseModes.NAMESPACED,
    )
    padding = "x" * 300
    instance = f"{doctype('doc', ('alpha',))}<doc><one>alpha</one><note>{padding}</note></doc>"

    # A tiny collector chunk window makes collection stop deep inside the
    # document, so the semantic parse must chain buffered prolog bytes
    # with the stream's remaining tail.
    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 32)
    document = schema.parse(NonSeekableBytes(instance.encode("utf-8")))

    assert not document.report.has_errors
    assert str(document.root.note) == padding


def test_replay_reader_breaks_when_a_read_ends_inside_the_buffer(
    monkeypatch,
) -> None:
    from pyxsd import dtd_declarations
    from pyxsd.schema import _PrologReplay

    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 1 << 20)
    document = b"<r>" + (b"y" * 40000) + b"</r>"
    replay = _PrologReplay(io.BytesIO(document))
    # The collector's single large read buffers the whole document.
    assert replay.read(1 << 20) == document
    replay.finish_collection()
    # A sized replay read ends inside the buffered chunk...
    assert replay.read(20000) == document[:20000]
    # ...and the next read resumes from the same chunk, then reports EOF
    # once buffer and stream are both exhausted.
    assert replay.read(1 << 20) == document[20000:]
    assert replay.read(1 << 20) == b""


def test_reaches_guard_ignores_non_type_candidates() -> None:
    from pyxsd.schema import _reaches_unparsed_entity_types

    assert _reaches_unparsed_entity_types("not-a-type", set()) is False


# ---------------------------------------------------------------------------
# Byte-budget boundary: declaration collection honours xml_limits.max_bytes
# ---------------------------------------------------------------------------


class CollectorReadSpy:
    """Counts the bytes the declaration collector reads from its source."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.collector_read = 0
        self.real = pyxsd.schema.collect_document_declarations
        spy = self

        class Counting:
            def __init__(self, source: Any) -> None:
                self._source = source

            def read(self, size: int = -1) -> Any:
                data = self._source.read(size)
                spy.collector_read += len(data)
                return data

        def counted(source: Any, *, policy: Any) -> DocumentDeclarations | None:
            return spy.real(Counting(source), policy=policy)

        monkeypatch.setattr(pyxsd.schema, "collect_document_declarations", counted)


def test_nonseekable_collection_is_capped_by_max_bytes(tmp_path: Path, monkeypatch) -> None:
    from pyxsd import dtd_declarations
    from pyxsd.resource_policy import ResourcePolicyError
    from pyxsd.xml_policy import XmlLimits

    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 32)
    spy = CollectorReadSpy(monkeypatch)
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    padding = "<!--" + ("p" * 400) + "-->"
    instance = f"{doctype('r', ('alpha',))}{padding}<r>alpha</r>".encode()

    # The prolog alone crosses the budget: collection must stop at the
    # cap, and the ordinary parser reports the standard limit failure.
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        schema.parse(BareReader(instance), xml_limits=XmlLimits(max_bytes=100))

    assert spy.collector_read <= 100 + 32


def test_seekable_rewind_collection_is_capped_by_max_bytes(tmp_path: Path, monkeypatch) -> None:
    from pyxsd import dtd_declarations
    from pyxsd.xml_policy import XmlLimits

    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 32)
    spy = CollectorReadSpy(monkeypatch)
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    instance = f"{doctype('r', ('alpha',))}<r>alpha</r>".encode()
    filler = b"<!--" + (b"f" * 512) + b"-->"  # a rootless oversized prolog

    class GrownOnRewind:
        """A seekable source whose bytes changed once collection rewinds."""

        def __init__(self) -> None:
            self._serving = io.BytesIO(instance)

        def read(self, size: int = -1) -> bytes:
            return self._serving.read(size)

        def seekable(self) -> bool:
            return True

        def tell(self) -> int:
            return self._serving.tell()

        def seek(self, offset: int, whence: int = 0) -> None:
            if offset == 0 and whence == 0:
                self._serving = io.BytesIO(filler)
            else:
                self._serving.seek(offset, whence)

    document = schema.parse(GrownOnRewind(), xml_limits=XmlLimits(max_bytes=len(instance)))

    assert not document.report.has_errors
    assert spy.collector_read <= len(instance) + 32


def test_top_level_limits_reach_element_collection(tmp_path: Path, monkeypatch) -> None:
    from pyxsd import dtd_declarations
    from pyxsd.xml_policy import XmlLimits

    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 32)
    spy = CollectorReadSpy(monkeypatch)
    schema_path = write(tmp_path, "schema.xsd", _ENTITY_ROOT_XSD)
    instance = f"{doctype('r', ('alpha',))}<r>alpha</r>".encode()
    # The caller's budget must cover both the schema document and the
    # instance; the oversized replacement prolog is far beyond it.
    filler = b"<!--" + (b"f" * 8192) + b"-->"

    class GrownOnRewind(io.RawIOBase):
        """Seekable: the real document first, oversized bytes after seek(0)."""

        def __init__(self) -> None:
            self._serving = io.BytesIO(instance)

        def readable(self) -> bool:
            return True

        def seekable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            return self._serving.read(size)

        def tell(self) -> int:
            return self._serving.tell()

        def seek(self, target: int, whence: int = 0) -> int:
            if target == 0 and whence == 0:
                self._serving = io.BytesIO(filler)
                return 0
            return self._serving.seek(target, whence)

    document = parse(GrownOnRewind(), xsd=schema_path, xml_limits=XmlLimits(max_bytes=4096))

    # Evidence degrades to lexical-only (the re-read crossed the budget),
    # but the collection itself never streamed past the caller's budget.
    assert not document.report.has_errors
    assert spy.collector_read <= 4096 + 32


def test_collection_collects_normally_at_the_exact_byte_budget(tmp_path: Path, monkeypatch) -> None:
    from pyxsd import dtd_declarations
    from pyxsd.xml_policy import XmlLimits

    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 32)
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")

    valid = f"{doctype('r', ('alpha',))}<r>alpha</r>".encode()
    document = schema.parse(BareReader(valid), xml_limits=XmlLimits(max_bytes=len(valid)))
    assert not document.report.has_errors

    invalid = f"{doctype('r', ('alpha',))}<r>omega</r>".encode()  # same length
    rejected = schema.parse(BareReader(invalid), xml_limits=XmlLimits(max_bytes=len(invalid)))
    assert rejected.report.has_errors


# ---------------------------------------------------------------------------
# Revalidation keeps the original parse's declaration evidence
# ---------------------------------------------------------------------------


def test_revalidate_preserves_a_declared_entity(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    document = parse_root_value(schema, tmp_path, "alpha", ("alpha",), "entity.xml")
    assert not document.report.has_errors
    # The serialized form carries no DOCTYPE; the evidence must come from
    # the original parse, not from what revalidation writes out.
    assert "<!DOCTYPE" not in document.to_string()

    again = document.revalidate()

    assert not again.report.has_errors
    assert not again.report.warnings


def test_revalidate_preserves_a_declared_entities_list(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITIES")
    document = parse_root_value(schema, tmp_path, "alpha beta", ("alpha", "beta"), "entities.xml")
    assert not document.report.has_errors

    again = document.revalidate()

    assert not again.report.has_errors
    assert not again.report.warnings


COMPLEX_ENTITY_XSD = (
    f"<?xml version='1.0'?>\n<xs:schema {XSD_NS}>\n"
    '  <xs:element name="doc"><xs:complexType><xs:sequence>\n'
    '    <xs:element name="note" type="xs:string"/>\n'
    '    <xs:element name="e" type="xs:ENTITY"/>\n'
    "  </xs:sequence></xs:complexType></xs:element>\n"
    "</xs:schema>\n"
)


def complex_entity_document(tmp_path: Path) -> Document:
    schema = Schema.compile(
        write(tmp_path, "schema.xsd", COMPLEX_ENTITY_XSD), mode=ParseModes.NAMESPACED
    )
    document = schema.parse(
        write(
            tmp_path,
            "entity.xml",
            f"{doctype('doc', ('alpha',))}<doc><note>n</note><e>alpha</e></doc>",
        )
    )
    assert not document.report.has_errors
    return document


def test_revalidate_after_a_transform_keeps_a_declared_entity(tmp_path: Path) -> None:
    document = complex_entity_document(tmp_path)

    def edit(tree: Any) -> Any:
        tree.note._value_ = ["changed"]
        return tree

    transformed = document.transform(edit)
    assert isinstance(transformed, Document)

    again = transformed.revalidate()

    assert not again.report.has_errors


def test_revalidate_keeps_complete_evidence_for_replaced_values(tmp_path: Path) -> None:
    """The carried evidence still rejects a new value it does not declare."""
    document = complex_entity_document(tmp_path)

    def replace(tree: Any) -> Any:
        tree.e._value_ = ["omega"]
        return tree

    transformed = document.transform(replace)
    assert isinstance(transformed, Document)

    again = transformed.revalidate()

    assert again.report.has_errors


def test_revalidate_without_evidence_stays_lexical_only(tmp_path: Path) -> None:
    """A document parsed without declaration evidence invents none."""
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    tree = ET.fromstring(f"{doctype('r', ('alpha',))}<r>omega</r>")
    document = schema.parse(tree)
    assert not document.report.has_errors

    again = document.revalidate()

    assert not again.report.has_errors


def test_revalidate_carries_incomplete_external_subset_evidence(tmp_path: Path) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    document = schema.parse(
        write(tmp_path, "external.xml", '<!DOCTYPE r SYSTEM "external.dtd"><r>omega</r>')
    )
    assert not document.report.has_errors
    assert [issue.code for issue in document.report.warnings].count("dtd-external-skipped") == 1

    again = document.revalidate()

    assert not again.report.has_errors
    assert [issue.code for issue in again.report.warnings].count("dtd-external-skipped") == 1


def test_revalidate_does_not_recollect_from_the_serialized_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    document = parse_root_value(schema, tmp_path, "alpha", ("alpha",), "entity.xml")
    spy = CollectionSpy(monkeypatch)

    document.revalidate()

    assert spy.calls == 0


def test_explicit_declarations_replace_element_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carried evidence is used for a pre-parsed Element, not collected."""
    schema = compile_entity_schema(tmp_path, "", "xs:ENTITY")
    tree = ET.fromstring(f"{doctype('r', ('alpha',))}<r>omega</r>")
    spy = CollectionSpy(monkeypatch)

    document = schema._parse_instance(
        tree, declarations=DocumentDeclarations(frozenset({"alpha"}), True)
    )

    assert spy.calls == 0
    assert document.report.has_errors
