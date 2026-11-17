"""Atomic typed assignment and joint compatibility for RELAX NG projections.

Every assignment prepares its lexical text and datatype context before it
touches the authoritative tree.  A failure therefore leaves names, attributes,
children, and revision freshness byte-for-byte unchanged.  Alternative
snapshots contribute lexical text and context only, never old validation
authority; a successful edit marks the whole revision stale until revalidation.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng import AlternativeValue, StaleInterpretationError
from pyxsd.relaxng.binding import (
    AttributeDescriptor,
    _encoding_pattern,
    bind_projection,
)
from pyxsd.relaxng.contracts import candidate_id, scalar_contract
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    UnsupportedDatatypeOperation,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.evidence import CandidateSelection, build_binding_evidence
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    Empty,
    Group,
    List,
    NotAllowed,
    OneOrMore,
    Ref,
)
from pyxsd.relaxng.projection import (
    _descriptor_encoding_state,
    _pattern_encoding_signature,
    _signature_sort_key,
    _stable_encoding_value,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.values import (
    _combine_list_tokens,
    _list_token_plan,
    encode_scalar,
    list_shape,
)

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
OPAQUE = "urn:pyxsd:test:opaque"
ENCODING = "urn:pyxsd:test:encoding"
CONFLICTING = "urn:pyxsd:test:conflicting"
SLOTTED = "urn:pyxsd:test:slotted"
NESTED = "urn:pyxsd:test:nested"
CYCLIC = "urn:pyxsd:test:cyclic"
UNREPRESENTABLE = "urn:pyxsd:test:unrepresentable"
INHERITED = "urn:pyxsd:test:inherited"
DUPLICATE = "urn:pyxsd:test:duplicate"
ITERATOR = "urn:pyxsd:test:iterator"
CYCLE_SHAPES = "urn:pyxsd:test:cycle-shapes"
MAPPING_MARKER = "urn:pyxsd:test:mapping-marker"
NESTED_CONTAINER = "urn:pyxsd:test:nested-container"
PRIMITIVE = "urn:pyxsd:test:primitive"


def data(type_name: str, library: str = XSD) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{library}"/>'


def element(name: str, content: str = "<empty/>") -> str:
    return f'<element name="{name}">{content}</element>'


def compile_body(body: str, **kwargs: object) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="doc">{body}</element>'), **kwargs
    )
    schema.require_valid()
    return schema


def typed(schema: RelaxNGSchema, xml: str, **kwargs: object):
    document = schema.parse(io.StringIO(xml), typed=True, **kwargs)  # type: ignore[arg-type]
    assert document.is_valid
    assert document.binding_evidence is not None
    return document


def serialized(document) -> str:
    return document.to_string()


def root_route(schema: RelaxNGSchema):
    return schema.projection.routes[schema.projection.roots[0]]


def scalar_site(graph, node):
    return next(site for site in graph.occurrences if site.element is node and site.kind == "text")


def selection_for(schema, graph, node, type_name: str) -> CandidateSelection:
    return selection_at(schema, graph, scalar_site(graph, node), type_name)


def selection_at(schema, graph, occurrence, type_name: str) -> CandidateSelection:
    witness = next(
        witness
        for witness in graph.scalar_witnesses_at(occurrence)
        if scalar_contract(witness.pattern).type_name == type_name
    )
    return CandidateSelection(
        occurrence,
        candidate_id(schema._grammar_fingerprint, scalar_contract(witness.pattern)),
    )


def attribute_site(graph, node, expanded_name: str):
    return next(
        site
        for site in graph.occurrences
        if site.element is node and site.kind == "attribute" and site.name == expanded_name
    )


def root_list_pattern(schema, document) -> List:
    graph = document.binding_evidence
    assert graph is not None
    site = scalar_site(graph, document.root)
    return next(pattern for pattern in graph._schema_patterns[site] if isinstance(pattern, List))


def encoding_pattern(schema, contract):
    return schema.projection.encoding_patterns[contract.pattern_identity]


class OpaqueDatatype(Datatype):
    """A validation-only datatype with no encoder capability."""

    library_uri = OPAQUE
    type_name = "opaque"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right


class OpaqueFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return OpaqueDatatype()


class EncodingCustomDatatype(Datatype):
    """A custom datatype whose encoder is available only from its pattern."""

    library_uri = ENCODING
    type_name = "custom"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"custom:{value}"


class EncodingCustomFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return EncodingCustomDatatype()


class VaryingEncoderDatatype(Datatype):
    """Same stable metadata, different encoder behavior per compiled instance."""

    library_uri = CONFLICTING
    type_name = "varying"
    parameters = ()

    def __init__(self, marker: str) -> None:
        self.state = {"marker": marker}

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"{self.state['marker']}:{value}"


class VaryingEncoderFactory(DatatypeFactory):
    def __init__(self) -> None:
        self.calls = 0

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        self.calls += 1
        return VaryingEncoderDatatype(str(self.calls))


@dataclass(frozen=True, slots=True)
class SlottedMarkerDatatype(Datatype):
    """Frozen+slots dataclass whose encoder behavior is inspectable state."""

    marker: str

    library_uri = SLOTTED
    type_name = "slotted"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"slotted:{self.marker}:{value}"


class SlottedMarkerFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return SlottedMarkerDatatype(next(self._markers))


class NestedSlottedBase:
    __slots__ = ("base_marker",)

    def __init__(self, base_marker: str) -> None:
        self.base_marker = base_marker


class NestedSlottedState(NestedSlottedBase):
    __slots__ = "marker"

    def __init__(self, base_marker: str, marker: str) -> None:
        super().__init__(base_marker)
        self.marker = marker


@dataclass(frozen=True, slots=True)
class NestedSlottedDatatype(Datatype):
    state: NestedSlottedState

    library_uri = NESTED
    type_name = "nested"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"nested:{self.state.base_marker}:{self.state.marker}:{value}"


class NestedSlottedFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return NestedSlottedDatatype(NestedSlottedState("base", next(self._markers)))


class CyclicState:
    __slots__ = ("marker", "self_ref")

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.self_ref = self


class CyclicStateDatatype(Datatype):
    library_uri = CYCLIC
    type_name = "cyclic"
    parameters = ()

    def __init__(self, marker: str) -> None:
        self.state = CyclicState(marker)

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"cyclic:{self.state.marker}:{value}"


class CyclicStateFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return CyclicStateDatatype("1")


class InheritedSlotBase:
    __slots__ = ("base_marker",)

    def __init__(self, base_marker: str) -> None:
        self.base_marker = base_marker


@dataclass(frozen=True, slots=True, eq=False)
class InheritedSlotDatatype(InheritedSlotBase, Datatype):
    marker: str

    library_uri = INHERITED
    type_name = "inherited"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"inherited:{self.base_marker}:{self.marker}:{value}"


class InheritedSlotFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        datatype = InheritedSlotDatatype("fixed")
        object.__setattr__(datatype, "base_marker", next(self._markers))
        return datatype


class DuplicateSlotBase:
    __slots__ = ("marker",)


class DuplicateSlotState(DuplicateSlotBase):
    __slots__ = ("marker",)

    def __init__(self, base_marker: str, derived_marker: str) -> None:
        DuplicateSlotBase.marker.__set__(self, base_marker)
        DuplicateSlotState.marker.__set__(self, derived_marker)


@dataclass(frozen=True, slots=True)
class DuplicateSlotDatatype(Datatype):
    state: DuplicateSlotState

    library_uri = DUPLICATE
    type_name = "duplicate"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        base_marker = DuplicateSlotBase.marker.__get__(self.state, type(self.state))
        return f"duplicate:{base_marker}:{value}"


class DuplicateSlotFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return DuplicateSlotDatatype(DuplicateSlotState(next(self._markers), "fixed-derived"))


class IteratorSlotState:
    __slots__ = iter(("marker",))

    def __init__(self, marker: str) -> None:
        self.marker = marker


@dataclass(frozen=True, slots=True)
class IteratorSlotDatatype(Datatype):
    state: IteratorSlotState

    library_uri = ITERATOR
    type_name = "iterator"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"iterator:{self.state.marker}:{value}"


class IteratorSlotFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return IteratorSlotDatatype(IteratorSlotState(next(self._markers)))


class CycleShapeNode:
    __slots__ = ("link", "marker")

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.link: object | None = None


@dataclass(frozen=True, slots=True)
class CycleShapeDatatype(Datatype):
    state: CycleShapeNode

    library_uri = CYCLE_SHAPES
    type_name = "cycle-shapes"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"cycle-shapes:{self.state.marker}:{value}"


def cycle_shape(shape: str) -> CycleShapeNode:
    tree = CycleShapeNode("root")
    child = CycleShapeNode("child")
    tree.link = child
    child.link = tree if shape == "ancestor" else child
    return tree


class CycleShapeFactory(DatatypeFactory):
    def __init__(self, shapes: tuple[str, ...]) -> None:
        self._shapes = iter(shapes)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return CycleShapeDatatype(cycle_shape(next(self._shapes)))


class MarkerDict(dict):
    def __init__(self, marker: str) -> None:
        super().__init__({"key": "value"})
        self.marker = marker


@dataclass(frozen=True, slots=True, eq=False)
class MappingMarkerDatatype(Datatype):
    state: dict

    library_uri = MAPPING_MARKER
    type_name = "mapping-marker"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"mapping-marker:{self.state.marker}:{value}"


class MappingMarkerFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return MappingMarkerDatatype(MarkerDict(next(self._markers)))


@dataclass(frozen=True, slots=True, eq=False)
class NestedContainerDatatype(Datatype):
    state: dict

    library_uri = NESTED_CONTAINER
    type_name = "nested-container"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return f"nested-container:{self.state['marker']}:{value}"


class NestedContainerFactory(DatatypeFactory):
    def __init__(self, markers: tuple[str, ...]) -> None:
        self._markers = iter(markers)

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        marker = next(self._markers)
        return NestedContainerDatatype(
            {
                "marker": marker,
                "mapping": MarkerDict(marker),
                "values": {"a", "b"},
                "sequence": [MarkerDict(marker)],
            }
        )


class UnrepresentableStateDatatype(Datatype):
    library_uri = UNREPRESENTABLE
    type_name = "unrepresentable"
    parameters = ()

    def __init__(self) -> None:
        self.callback = lambda value: value

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del context
        return str(self.callback(value))


class UnrepresentableStateFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return UnrepresentableStateDatatype()


class MarkerEnum(Enum):
    ONE = "1"


class MarkerString(str):
    def __new__(cls, marker):
        value = super().__new__(cls, "payload")
        value.marker = marker
        return value


class SlottedStringBase(str):
    __slots__ = ("marker",)


class SlottedMarkerString(SlottedStringBase):
    __slots__ = ("extra",)

    def __new__(cls, marker):
        value = super().__new__(cls, "payload")
        value.marker = marker
        value.extra = "fixed"
        return value


@dataclass(frozen=True, slots=True)
class PrimitivePayloadDatatype(Datatype):
    state: object

    library_uri = PRIMITIVE
    type_name = "primitive"
    parameters = ()

    def __eq__(self, other):
        return (
            type(other) is type(self)
            and self.state == other.state
            and self.state.marker == other.state.marker
        )

    def allows(self, lexical, context):
        return True

    def equal(self, left, left_context, right, right_context):
        return left == right

    def encode(self, value, context):
        return f"{self.state.marker}:{value}"


class PrimitivePayloadFactory(DatatypeFactory):
    def __init__(self, payloads):
        self.payloads = iter(payloads)

    def create(self, type_name, parameters, context):
        return PrimitivePayloadDatatype(next(self.payloads))


def compile_primitive_payloads(payloads):
    return compile_body(
        encoded_two_child_schema(PRIMITIVE, "primitive"),
        datatype_libraries={
            PRIMITIVE: DatatypeLibraryRegistration(
                PrimitivePayloadFactory(payloads), "primitive/1"
            ),
        },
    )


class MissingSlotState:
    __slots__ = ("marker",)


class PrivateSlotState:
    __slots__ = ("__marker",)

    def __init__(self, marker: str) -> None:
        self.__marker = marker


class SkippedSlotState:
    __slots__ = ("__dict__", "__weakref__", "marker")

    def __init__(self, marker: str) -> None:
        self.marker = marker


@dataclass(frozen=True, slots=True)
class PureSlotsState:
    marker: str


@dataclass
class ExtraDictState:
    marker: str


class NonStringEncoderDatatype(Datatype):
    library_uri = OPAQUE
    type_name = "nonstring"
    parameters = ()

    def allows(self, lexical, context):
        del lexical, context
        return True

    def equal(self, left, left_context, right, right_context):
        del left_context, right_context
        return left == right

    def encode(self, value, context):
        del value, context
        return 1  # type: ignore[return-value]


def test_unique_text_descriptor_assignment_encodes_before_serialization():
    schema = compile_body('<attribute name="a"><text/></attribute><text/>')
    document = typed(schema, '<doc a="old">text</doc>')
    root = document.root

    root.assign(a="new", typed_value="replacement")

    assert serialized(document) == '<doc a="new">replacement</doc>'
    assert document.interpretations_current is False
    assert root.revision.dirty is True
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value
    with pytest.raises(StaleInterpretationError):
        _ = root.a


def test_unique_atomic_scalar_and_attribute_assignment_encodes_values():
    schema = compile_body(
        '<attribute name="n">' + data("integer") + "</attribute>" + data("integer")
    )
    document = typed(schema, '<doc n="1">2</doc>')
    root = document.root

    root.assign(n=5, typed_value=7)

    assert serialized(document) == '<doc n="5">7</doc>'
    assert root.revision.dirty is True


def test_optional_attribute_and_absent_scalar_can_be_created_by_assignment():
    schema = compile_body(
        '<optional><attribute name="n">'
        + data("integer")
        + "</attribute></optional><optional>"
        + data("integer")
        + "</optional>"
    )
    document = typed(schema, "<doc/>")
    root = document.root

    assert root.n is None
    assert root.typed_value is None
    root.assign(n=4, typed_value=9)

    assert serialized(document) == '<doc n="4">9</doc>'


def test_text_assignment_requires_a_string_and_never_stringifies_objects():
    schema = compile_body("<text/>")
    document = typed(schema, "<doc>old</doc>")
    root = document.root
    before = serialized(document)

    for value in (1, True, object(), b"bytes"):
        with pytest.raises(TypeError):
            root.typed_value = value
        assert serialized(document) == before
        assert root.revision.dirty is False

    root.typed_value = "new"
    assert serialized(document) == "<doc>new</doc>"


@pytest.mark.parametrize("type_name", ["integer", "decimal", "float", "double"])
def test_bool_is_rejected_as_a_numeric_datatype_value(type_name):
    schema = compile_body(f'<attribute name="n">{data(type_name)}</attribute><empty/>')
    document = typed(schema, '<doc n="1"/>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="expected"):
        root.n = True
    assert serialized(document) == before
    assert document.interpretations_current is True

    contract = root_route(schema).attributes["n"].scalar_contracts[0]
    with pytest.raises(TypeError, match="expected"):
        encode_scalar(
            contract,
            True,
            DatatypeContext(None, {}),
            pattern=encoding_pattern(schema, contract),
        )


def test_encode_scalar_uses_explicit_contracts_without_str_fallback():
    schema = compile_body("<text/>")
    typed(schema, "<doc>value</doc>")
    contract = root_route(schema).content.scalar_contracts[0]
    context = DatatypeContext(None, {})

    assert contract.kind == "text"
    pattern = encoding_pattern(schema, contract)
    assert encode_scalar(contract, "value", context, pattern=pattern) == "value"
    for value in (1, True, object()):
        with pytest.raises(TypeError):
            encode_scalar(contract, value, context, pattern=pattern)


def test_missing_atomic_encoder_is_a_specific_capability_error_and_atomic():
    schema = compile_body(
        f'<attribute name="value">{data("opaque", OPAQUE)}</attribute><empty/>',
        datatype_libraries={
            OPAQUE: DatatypeLibraryRegistration(OpaqueFactory(), "opaque/1"),
        },
    )
    document = typed(schema, '<doc value="original"/>')
    root = document.root
    before = serialized(document)

    with pytest.raises(UnsupportedDatatypeOperation, match="encoder"):
        root.value = "replacement"

    assert serialized(document) == before
    assert root.revision.dirty is False
    contract = root_route(schema).attributes["value"].scalar_contracts[0]
    with pytest.raises(UnsupportedDatatypeOperation, match="encoder"):
        encode_scalar(
            contract,
            "replacement",
            DatatypeContext(None, {}),
            pattern=encoding_pattern(schema, contract),
        )


def test_ambiguous_bare_values_are_rejected_but_snapshots_are_lexical_edits():
    schema = compile_body(f"<choice>{data('integer')}{data('string')}</choice>")
    document = typed(schema, "<doc>12</doc>")
    root = document.root
    snapshot = root.typed_value
    assert isinstance(snapshot, AlternativeValue)
    before = serialized(document)

    for value in (12, "12", 12.0, object()):
        with pytest.raises(relaxng.AmbiguousValueError):
            root.typed_value = value
        assert serialized(document) == before
        assert document.interpretations_current is True

    root.assign(typed_value=snapshot)

    assert serialized(document) == before
    assert document.interpretations_current is False
    assert snapshot.as_type("xsd:integer") == 12
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value


def test_alternative_snapshot_grammar_and_context_must_match_the_document():
    schema = compile_body(
        '<attribute name="q"><choice>'
        + data("QName")
        + data("string")
        + "</choice></attribute><empty/>"
    )
    document = typed(schema, '<doc xmlns:p="urn:p" q="p:value"/>')
    root = document.root
    snapshot = root.q
    assert isinstance(snapshot, AlternativeValue)
    before = serialized(document)

    wrong_grammar = replace(snapshot, grammar_fingerprint="urn:other-grammar")
    wrong_namespace = replace(snapshot, context=DatatypeContext(None, {"p": "urn:different"}))
    wrong_base = replace(
        snapshot,
        context=DatatypeContext("https://example.test/base", dict(snapshot.context.namespaces)),
    )
    foreign_revision = replace(snapshot, revision=snapshot.revision + 100)

    for candidate in (wrong_grammar, wrong_namespace, wrong_base):
        with pytest.raises(ValueError, match=r"grammar|context|namespace|base"):
            root.q = candidate
        assert serialized(document) == before
        assert document.interpretations_current is True

    # A prior revision is an allowed lexical source: its text and context are
    # reusable, but its candidates never become fresh validation evidence.
    root.q = foreign_revision
    assert serialized(document) == before
    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = root.q


def test_unique_homogeneous_list_assignment_uses_schema_item_contracts():
    schema = compile_body("<list><oneOrMore>" + data("integer") + "</oneOrMore></list>")
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    assert root.typed_value == [1, 2]

    root.typed_value = [3, 4]

    assert serialized(document) == "<doc>3 4</doc>"
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value


def test_unique_fixed_tuple_assignment_requires_its_declared_shape():
    schema = compile_body("<list><group>" + data("integer") + data("string") + "</group></list>")
    document = typed(schema, "<doc>1 one</doc>")
    root = document.root
    assert root.typed_value == (1, "one")

    for value in ([2, "two"], ("two", 2), (2,), "2 two", object()):
        with pytest.raises((TypeError, ValueError)):
            root.typed_value = value
        assert serialized(document) == "<doc>1 one</doc>"
        assert document.interpretations_current is True

    root.typed_value = (2, "two")
    assert serialized(document) == "<doc>2 two</doc>"
    assert document.interpretations_current is False


def test_repeated_equal_item_patterns_share_one_compiled_plan_entry():
    schema = compile_body("<list><group>" + data("integer") + data("integer") + "</group></list>")
    document = typed(schema, "<doc>1 2</doc>")

    document.root.typed_value = (3, 4)

    assert serialized(document) == "<doc>3 4</doc>"


def test_homogeneous_list_shape_rejects_tuple_and_arbitrary_objects():
    schema = compile_body("<list><oneOrMore>" + data("integer") + "</oneOrMore></list>")
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    before = serialized(document)

    for value in ((3, 4), "3 4", b"3 4", object()):
        with pytest.raises((TypeError, ValueError)):
            root.typed_value = value
        assert serialized(document) == before
        assert document.interpretations_current is True

    root.typed_value = [3, 4, 5]
    assert serialized(document) == "<doc>3 4 5</doc>"


@pytest.mark.parametrize(
    "item",
    ["a b", "", "a\tb", "a\rb", "a\nb"],
    ids=["space", "empty", "tab", "carriage-return", "line-feed"],
)
def test_homogeneous_list_items_cannot_contain_encoded_xml_whitespace(item):
    schema = compile_body("<list><oneOrMore>" + data("string") + "</oneOrMore></list>")
    document = typed(schema, "<doc>seed</doc>")
    root = document.root
    before = serialized(document)

    with pytest.raises(ValueError, match=r"empty|XML whitespace"):
        root.typed_value = [item]

    assert serialized(document) == before
    assert document.interpretations_current is True


@pytest.mark.parametrize("item", ["a b", "", "a\tb"])
def test_fixed_tuple_items_cannot_contain_encoded_xml_whitespace(item):
    schema = compile_body("<list><group>" + data("string") + data("integer") + "</group></list>")
    document = typed(schema, "<doc>seed 1</doc>")
    root = document.root
    before = serialized(document)

    with pytest.raises(ValueError, match=r"empty|XML whitespace"):
        root.typed_value = (item, 2)

    assert serialized(document) == before
    assert document.interpretations_current is True


def test_nbsp_is_a_legal_list_token_character_and_empty_sequences_are_allowed():
    string_schema = compile_body("<list><oneOrMore>" + data("string") + "</oneOrMore></list>")
    string_document = typed(string_schema, "<doc>seed</doc>")
    string_document.root.typed_value = ["a\u00a0b"]
    assert serialized(string_document) == "<doc>a\u00a0b</doc>"

    empty_schema = compile_body("<list><zeroOrMore>" + data("integer") + "</zeroOrMore></list>")
    empty_document = typed(empty_schema, "<doc>1 2</doc>")
    empty_document.root.typed_value = []
    assert serialized(empty_document) == "<doc></doc>"


def test_ambiguous_list_item_contracts_report_the_structural_gap():
    schema = compile_body(
        "<list><oneOrMore><choice>"
        + data("integer")
        + data("string")
        + "</choice></oneOrMore></list>"
    )
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    assert isinstance(root.typed_value, AlternativeValue)
    before = serialized(document)

    with pytest.raises(UnsupportedDatatypeOperation, match=r"token|structural"):
        root.typed_value = [1, 2]

    assert serialized(document) == before
    assert document.interpretations_current is True
    contract = root_route(schema).content.scalar_contracts[0]
    with pytest.raises(UnsupportedDatatypeOperation, match=r"token|structural"):
        encode_scalar(
            contract,
            [1, 2],
            DatatypeContext(None, {}),
            pattern=encoding_pattern(schema, contract),
        )


def test_all_encodings_are_prepared_before_any_mutation():
    schema = compile_body(
        '<attribute name="n">' + data("integer") + "</attribute>" + data("integer")
    )
    document = typed(schema, '<doc n="1">2</doc>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="expected"):
        root.assign(n=5, typed_value=True)

    assert serialized(document) == before
    assert document.interpretations_current is True
    assert root.n == 1
    assert root.typed_value == 2

    with pytest.raises(TypeError, match="expected"):
        root.assign(n=5, typed_value=object())

    assert serialized(document) == before
    assert root.revision.dirty is False


def test_unknown_and_child_structural_assignments_are_explicit():
    schema = compile_body(
        element("item", data("integer")) + '<attribute name="a"><text/></attribute>'
    )
    document = typed(schema, '<doc a="x"><item>1</item></doc>')
    root = document.root

    with pytest.raises(TypeError, match="field"):
        root.assign(missing=1)
    with pytest.raises(TypeError, match="field"):
        root.assign(nope=1)

    root.item = 4
    assert serialized(document) == '<doc a="x"><item>4</item></doc>'


def test_child_list_descriptor_assignment_encodes_tokens_before_mutation():
    schema = compile_body(
        element("items", "<list><oneOrMore>" + data("integer") + "</oneOrMore></list>")
    )
    document = typed(schema, "<doc><items>1 2</items></doc>")
    root = document.root

    root.items = [3, 4]

    assert serialized(document) == "<doc><items>3 4</items></doc>"


def test_descriptor_suffix_and_typed_value_collision_path_assigns_both_positions():
    schema = compile_body(
        '<attribute name="typed_value"><text/></attribute>'
        '<attribute name="typed_value_"><text/></attribute>' + data("integer")
    )
    document = typed(schema, '<doc typed_value="a" typed_value_="b">12</doc>')
    root = document.root
    assert type(root)._relaxng_scalar_field_ == "typed_value__"

    root.assign(typed_value="A", typed_value__=13)

    assert serialized(document) == '<doc typed_value="A" typed_value_="b">13</doc>'
    with pytest.raises(StaleInterpretationError):
        _ = root.typed_value__


def collision_schema() -> RelaxNGSchema:
    return compile_body(
        '<attribute name="assign"><text/></attribute>'
        '<attribute name="assign_fields"><text/></attribute>'
        '<attribute name="selections"><text/></attribute>' + data("integer")
    )


def test_transaction_names_are_reserved_and_descriptor_writes_are_collision_safe():
    schema = collision_schema()
    document = typed(
        schema,
        '<doc assign="a" assign_fields="b" selections="c">1</doc>',
    )
    root = document.root

    assert callable(type(root).assign)
    assert callable(type(root).assign_fields)
    assert isinstance(type(root).assign_2, AttributeDescriptor)
    assert isinstance(type(root).assign_fields_2, AttributeDescriptor)
    assert isinstance(type(root).selections, AttributeDescriptor)

    root.assign_2 = "A"
    root.assign_fields_2 = "B"
    root.selections = ""
    root.typed_value = 5

    assert serialized(document) == ('<doc assign="A" assign_fields="B" selections="">5</doc>')
    assert root.attribute_lexical("selections") == ""
    assert root.revision.dirty is True


def test_mapping_transaction_writes_reserved_and_selection_field_names():
    schema = collision_schema()
    document = typed(
        schema,
        '<doc assign="a" assign_fields="b" selections="c">1</doc>',
    )
    root = document.root

    root.assign_fields(
        {
            "assign_2": "A",
            "assign_fields_2": "B",
            "selections": "",
            "typed_value": 7,
        }
    )

    assert serialized(document) == ('<doc assign="A" assign_fields="B" selections="">7</doc>')
    assert root.attribute_lexical("selections") == ""
    assert document.interpretations_current is False


def test_convenience_assign_still_works_when_a_sibling_field_is_named_assign():
    schema = collision_schema()
    document = typed(
        schema,
        '<doc assign="a" assign_fields="b" selections="c">1</doc>',
    )

    document.root.assign(assign_2="renamed", typed_value=8)

    assert serialized(document) == (
        '<doc assign="renamed" assign_fields="b" selections="c">8</doc>'
    )


def test_mapping_and_convenience_assignment_reject_unknown_keys():
    document = typed(collision_schema(), '<doc assign="a" assign_fields="b" selections="c">1</doc>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="unknown"):
        root.assign_fields({"missing": 1})
    with pytest.raises(TypeError, match="unknown"):
        root.assign(missing=1)
    with pytest.raises(TypeError, match="string field names"):
        root.assign_fields({1: 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="mapping"):
        root.assign_fields(["typed_value"])  # type: ignore[arg-type]

    assert serialized(document) == before
    assert document.interpretations_current is True


def test_generated_class_without_a_compiled_encoding_plan_cannot_assign():
    document = typed(compile_body("<text/>"), "<doc>value</doc>")
    root = document.root
    generated = type(root)
    saved = generated._relaxng_encoding_patterns_
    del generated._relaxng_encoding_patterns_
    try:
        with pytest.raises(PyXSDError, match="compiled projection plan"):
            root.typed_value = "next"
    finally:
        generated._relaxng_encoding_patterns_ = saved

    assert serialized(document) == "<doc>value</doc>"
    assert document.interpretations_current is True


def test_descriptor_writes_share_the_transaction_path():
    schema = compile_body('<attribute name="a"><text/></attribute>' + data("integer"))
    document = typed(schema, '<doc a="old">1</doc>')
    root = document.root

    root.a = "new"
    root.typed_value = 2

    assert serialized(document) == '<doc a="new">2</doc>'
    assert document.interpretations_current is False


def test_lexical_writes_remain_available_after_typed_assignment():
    schema = compile_body(
        '<attribute name="a">' + data("integer") + "</attribute>" + data("integer")
    )
    document = typed(schema, '<doc a="1">2</doc>')
    root = document.root

    root.typed_value = 3
    root.set_text("lexical")
    root.set_attribute("a", "spelled-lexically")

    assert serialized(document) == '<doc a="spelled-lexically">lexical</doc>'
    assert root.text_value() == "lexical"
    assert root.attribute_lexical("a") == "spelled-lexically"


def correlated_document():
    choice = (
        "<choice><group>"
        + element("x", data("integer"))
        + element("y", data("integer"))
        + "</group><group>"
        + element("x", data("string"))
        + element("y", data("string"))
        + "</group></choice>"
    )
    schema = compile_body(choice)
    document = typed(schema, "<doc><x>12</x><y>34</y></doc>")
    return schema, document


def test_jointly_incompatible_selections_fail_before_any_mutation():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, y = root.children
    xi = selection_for(schema, graph, x, "integer")
    ys = selection_for(schema, graph, y, "string")
    before = serialized(document)

    snapshot_x = x.typed_value
    snapshot_y = y.typed_value
    with pytest.raises(ValueError, match=r"joint|compatible"):
        root.assign(x=snapshot_x, y=snapshot_y, selections=(xi, ys))

    assert serialized(document) == before
    assert document.interpretations_current is True


def test_selections_must_belong_to_an_assigned_current_field():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, y = root.children
    yi = selection_for(schema, graph, y, "integer")
    snapshot_x = x.typed_value
    before = serialized(document)

    with pytest.raises(ValueError, match=r"assigned field|field occurrence"):
        root.assign(x=snapshot_x, selections=(yi,))
    assert serialized(document) == before

    other_schema, other_document = correlated_document()
    other_graph = other_document.binding_evidence
    assert other_graph is not None
    other_yi = selection_for(other_schema, other_graph, other_document.root.children[1], "integer")
    with pytest.raises(ValueError, match=r"current|assigned field|field occurrence"):
        root.assign(x=snapshot_x, selections=(other_yi,))
    assert serialized(document) == before


def test_valid_joint_selections_commit_snapshots_from_an_old_revision():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, y = root.children
    xi = selection_for(schema, graph, x, "integer")
    yi = selection_for(schema, graph, y, "integer")

    other_document = typed(schema, "<doc><x>99</x><y>88</y></doc>")
    snapshot_x = other_document.root.children[0].typed_value
    snapshot_y = other_document.root.children[1].typed_value

    root.assign(x=snapshot_x, y=snapshot_y, selections=(xi, yi))

    assert serialized(document) == "<doc><x>99</x><y>88</y></doc>"
    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        _ = x.typed_value
    with pytest.raises(StaleInterpretationError):
        _ = y.typed_value


def test_a_selection_may_target_a_token_of_an_assigned_list_field():
    schema = compile_body(
        "<list><oneOrMore><choice>"
        + data("integer")
        + data("string")
        + "</choice></oneOrMore></list>"
    )
    document = typed(schema, "<doc>1 2</doc>")
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    token = next(
        site for site in graph.occurrences if site.kind == "token" and site.token_index == 0
    )
    selected = selection_at(schema, graph, token, "integer")
    snapshot = root.typed_value
    assert isinstance(snapshot, AlternativeValue)

    root.assign(typed_value=snapshot, selections=(selected,))

    assert serialized(document) == "<doc>1 2</doc>"
    assert document.interpretations_current is False


def test_selections_after_a_mutation_are_stale():
    schema, document = correlated_document()
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    x, _ = root.children
    xi = selection_for(schema, graph, x, "integer")
    snapshot_x = x.typed_value

    root.set_text(" ")
    with pytest.raises(StaleInterpretationError):
        root.assign(x=snapshot_x, selections=(xi,))


def test_typed_assignment_marks_annotations_stale_when_captured():
    schema = compile_body("<text/>")
    document = typed(schema, "<doc>old</doc>", capture="patterns")
    root = document.root

    root.typed_value = "new"

    with pytest.raises(StaleInterpretationError):
        _ = document.annotations
    assert serialized(document) == "<doc>new</doc>"


def test_builtin_atomic_contract_uses_its_compiled_encoder():
    schema = compile_body('<attribute name="v">' + data("token", "") + "</attribute><empty/>")
    typed(schema, '<doc v="a"/>')
    contract = root_route(schema).attributes["v"].scalar_contracts[0]
    context = DatatypeContext(None, {})
    pattern = encoding_pattern(schema, contract)

    assert encode_scalar(contract, " a\tb ", context, pattern=pattern) == "a b"
    assert encode_scalar(contract, "plain", context, pattern=pattern) == "plain"


def test_encode_scalar_rejects_wrong_contract_and_context_types():
    schema = compile_body("<text/>")
    contract = root_route(schema).content.scalar_contracts[0]
    context = DatatypeContext(None, {})

    pattern = encoding_pattern(schema, contract)
    with pytest.raises(TypeError, match="ScalarContract"):
        encode_scalar(object(), "x", context, pattern=pattern)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DatatypeContext"):
        encode_scalar(contract, "x", object(), pattern=pattern)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="compiled pattern"):
        encode_scalar(contract, "x", context, pattern=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="scalar pattern"):
        encode_scalar(contract, "x", context, pattern=schema.grammar.start)
    integer_schema = compile_body(data("integer"))
    integer_contract = root_route(integer_schema).content.scalar_contracts[0]
    with pytest.raises(ValueError, match="does not implement"):
        encode_scalar(integer_contract, 1, context, pattern=pattern)
    with pytest.raises(ValueError, match="compile-time encoding pattern"):
        _encoding_pattern({}, contract, "test position")


def test_a_datatype_encoder_must_return_lexical_text():
    schema = compile_body(
        f'<attribute name="v">{data("nonstring", OPAQUE)}</attribute><empty/>',
        datatype_libraries={
            OPAQUE: DatatypeLibraryRegistration(NonStringEncoderDatatypeFactory(), "opaque/2"),
        },
    )
    document = typed(schema, '<doc v="original"/>')
    root = document.root
    before = serialized(document)

    with pytest.raises(TypeError, match="lexical string"):
        root.v = "replacement"

    assert serialized(document) == before
    assert document.interpretations_current is True


class NonStringEncoderDatatypeFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return NonStringEncoderDatatype()


def test_optional_absent_list_reuses_its_compiled_token_pattern():
    schema = compile_body(
        "<optional><list><oneOrMore>" + data("integer") + "</oneOrMore></list></optional>"
    )
    document = typed(schema, "<doc/>")
    root = document.root
    assert root.typed_value is None

    root.typed_value = [1]

    assert serialized(document) == "<doc>1</doc>"


def test_bare_value_without_any_scalar_contract_is_rejected():
    schema = compile_body('<attribute name="empty"><empty/></attribute><empty/>')
    document = typed(schema, '<doc empty=""/>')
    root = document.root

    with pytest.raises(ValueError, match="no schema scalar contract"):
        root.empty = "value"

    assert serialized(document) == '<doc empty=""/>'
    assert document.interpretations_current is True


def test_repeatable_and_absent_child_assignments_are_explicit_boundaries():
    repeated = compile_body("<zeroOrMore>" + element("many", data("integer")) + "</zeroOrMore>")
    repeated_document = typed(repeated, "<doc/>")
    with pytest.raises(TypeError, match=r"repeatable|Task 4"):
        repeated_document.root.many = [1]

    optional = compile_body("<optional>" + element("single", data("integer")) + "</optional>")
    optional_document = typed(optional, "<doc/>")
    with pytest.raises(ValueError, match=r"exactly one existing|Task 4"):
        optional_document.root.single = 1


def test_selections_allow_an_absent_assigned_attribute_next_to_a_selected_one():
    schema = compile_body(
        '<attribute name="a">'
        + data("integer")
        + '</attribute><optional><attribute name="b">'
        + data("integer")
        + "</attribute></optional><empty/>"
    )
    document = typed(schema, '<doc a="1"/>')
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    selected = selection_at(schema, graph, attribute_site(graph, root, "a"), "integer")

    root.assign(a=2, b=3, selections=(selected,))

    assert serialized(document) == '<doc a="2" b="3"/>'


def test_empty_assignment_is_a_noop_and_selection_arguments_are_validated():
    schema = compile_body("<text/>")
    document = typed(schema, "<doc>value</doc>")
    root = document.root
    graph = document.binding_evidence
    assert graph is not None
    selected = selection_for(schema, graph, root, None)  # text witness has no type name

    assert root.assign() is None
    assert root.assign(selections=()) is None
    assert document.interpretations_current is True
    with pytest.raises(ValueError, match="at least one assigned field"):
        root.assign(selections=(selected,))
    with pytest.raises(TypeError, match="iterable"):
        root.assign(typed_value="next", selections=selected)
    with pytest.raises(TypeError, match="CandidateSelection"):
        root.assign(typed_value="next", selections=(object(),))
    assert serialized(document) == "<doc>value</doc>"


def test_generic_and_untyped_projected_elements_reject_typed_assignment():
    schema = compile_body("<text/>")
    validation = schema.parse(io.StringIO("<doc>value</doc>"), typed=False)
    with pytest.raises(PyXSDError, match="projected"):
        validation.root.assign(typed_value="next")

    root = RelaxNGElement.from_etree(ET.fromstring("<doc>value</doc>"))
    evidence = build_binding_evidence(
        root,
        schema.grammar.start,
        datatype_context=DatatypeContext(None, {}),
        limits=None,
        projection_fingerprint=schema.projection.grammar_fingerprint,
    )
    bind_projection(root, schema.projection, evidence)
    with pytest.raises(PyXSDError, match="valid typed parse"):
        root.assign(typed_value="next")


def test_optional_absent_custom_attribute_reports_no_encoder():
    schema = compile_body(
        '<optional><attribute name="v">'
        + data("opaque", OPAQUE)
        + "</attribute></optional><empty/>",
        datatype_libraries={
            OPAQUE: DatatypeLibraryRegistration(OpaqueFactory(), "opaque/1"),
        },
    )
    document = typed(schema, "<doc/>")
    root = document.root

    with pytest.raises(UnsupportedDatatypeOperation, match="encoder"):
        root.v = "value"

    assert serialized(document) == "<doc/>"


def test_plan_encoding_registry_covers_absent_optional_positions():
    schema = compile_body(
        '<optional><attribute name="n"><list><oneOrMore>'
        + data("integer")
        + "</oneOrMore></list></attribute></optional>"
        + '<optional><attribute name="v">'
        + data("custom", ENCODING)
        + "</attribute></optional><empty/>",
        datatype_libraries={
            ENCODING: DatatypeLibraryRegistration(EncodingCustomFactory(), "encoding/1"),
        },
    )
    plan = schema.projection
    contracts = tuple(
        contract
        for field in root_route(schema).attributes.values()
        for contract in field.scalar_contracts
    )

    assert contracts
    assert {contract.pattern_identity for contract in contracts} <= set(plan.encoding_patterns)
    assert all(
        scalar_contract(plan.encoding_patterns[contract.pattern_identity]) == contract
        for contract in contracts
    )


def test_absent_optional_list_uses_the_compile_time_registry():
    schema = compile_body(
        '<optional><attribute name="n"><list><oneOrMore>'
        + data("integer")
        + "</oneOrMore></list></attribute></optional><empty/>"
    )
    document = typed(schema, "<doc/>")

    assert document.root.attribute_lexical("n") is None
    document.root.n = [2, 3]

    assert serialized(document) == '<doc n="2 3"/>'


def test_absent_custom_encoder_uses_its_compile_time_pattern():
    schema = compile_body(
        '<optional><attribute name="v">'
        + data("custom", ENCODING)
        + "</attribute></optional><empty/>",
        datatype_libraries={
            ENCODING: DatatypeLibraryRegistration(EncodingCustomFactory(), "encoding/1"),
        },
    )
    document = typed(schema, "<doc/>")

    document.root.v = "value"

    assert serialized(document) == '<doc v="custom:value"/>'


def test_unrelated_present_occurrence_does_not_change_encoded_target_bytes():
    schema = compile_body(
        element("target", data("integer"))
        + "<optional>"
        + element("probe", data("integer"))
        + "</optional>"
    )
    absent = typed(schema, "<doc><target>0</target></doc>")
    present = typed(schema, "<doc><target>0</target><probe>9</probe></doc>")

    absent.root.target.typed_value = 5
    present.root.target.typed_value = 5

    from pyxsd.relaxng.xml_writer import serialize_relaxng

    assert (
        serialize_relaxng(absent.root.target)
        == serialize_relaxng(present.root.target)
        == "<target>5</target>"
    )


def test_encoding_registry_is_stable_across_repeated_compiles_and_parses():
    body = (
        '<optional><attribute name="n"><list><oneOrMore>'
        + data("integer")
        + "</oneOrMore></list></attribute></optional><empty/>"
    )
    first = compile_body(body)
    second = compile_body(body)

    first_patterns = {
        identity: scalar_contract(pattern)
        for identity, pattern in first.projection.encoding_patterns.items()
    }
    second_patterns = {
        identity: scalar_contract(pattern)
        for identity, pattern in second.projection.encoding_patterns.items()
    }
    assert first_patterns == second_patterns

    first_document = typed(first, "<doc/>")
    second_document = typed(second, "<doc/>")
    first_document.root.assign(n=[4, 5])
    second_document.root.assign(n=[4, 5])
    assert serialized(first_document) == serialized(second_document) == '<doc n="4 5"/>'


def test_conflicting_encoding_patterns_fail_deterministically_at_compile_time():
    body = (
        "<choice>"
        + element("first", '<attribute name="v">' + data("varying", CONFLICTING) + "</attribute>")
        + element("second", '<attribute name="v">' + data("varying", CONFLICTING) + "</attribute>")
        + "</choice>"
    )
    messages = []
    for _ in range(2):
        with pytest.raises(ValueError, match="conflicting RELAX NG encoding patterns") as caught:
            compile_body(
                body,
                datatype_libraries={
                    CONFLICTING: DatatypeLibraryRegistration(
                        VaryingEncoderFactory(), "conflicting/1"
                    ),
                },
            )
        messages.append(str(caught.value))
    assert messages[0] == messages[1]


def encoded_two_child_schema(library: str, type_name: str) -> str:
    encoded = data(type_name, library)
    return (
        "<choice>"
        + element("first", '<attribute name="v">' + encoded + "</attribute>")
        + element("second", '<attribute name="v">' + encoded + "</attribute>")
        + "</choice>"
    )


@pytest.mark.parametrize("payload_type", [MarkerString, SlottedMarkerString])
def test_primitive_subclass_marker_conflicts_fail_before_assignment(payload_type):
    first, second = payload_type("1"), payload_type("2")
    assert str(first) == str(second) == "payload"
    assert PrimitivePayloadDatatype(first) != PrimitivePayloadDatatype(second)
    with pytest.raises(ValueError, match="conflicting RELAX NG encoding patterns"):
        compile_primitive_payloads((first, second))


@pytest.mark.parametrize("payload_type", [MarkerString, SlottedMarkerString])
def test_equal_primitive_subclass_markers_compile_and_assign(payload_type):
    schema = compile_primitive_payloads((payload_type("1"), payload_type("1")))
    for field in ("first", "second"):
        document = typed(schema, f'<doc><{field} v="seed"/></doc>')
        getattr(document.root, field).v = "value"
        assert serialized(document) == f'<doc><{field} v="1:value"/></doc>'


def test_primitive_subclass_opaque_state_fails_closed_with_contract_context():
    payload = MarkerString("1")
    payload.callback = lambda value: value
    with pytest.raises(
        ValueError, match="cannot represent RELAX NG encoding state for scalar contract identity"
    ):
        compile_primitive_payloads((payload, MarkerString("1")))


@pytest.mark.parametrize(
    "base, first, second",
    [
        (str, "a", "b"),
        (int, 1, 2),
        (float, 1.5, 2.5),
        (complex, 1j, 2j),
        (bytes, b"a", b"b"),
        (bytearray, b"a", b"b"),
    ],
)
def test_primitive_subclass_signatures_preserve_type_payload_and_state(base, first, second):
    subclass = type("Payload", (base,), {})
    other_subclass = type("OtherPayload", (base,), {})
    left, equal, different = subclass(first), subclass(first), subclass(second)
    other = other_subclass(first)
    left.marker = equal.marker = different.marker = "1"
    other.marker = "1"
    signature = _stable_encoding_value(left)
    assert signature == _stable_encoding_value(equal)
    assert signature != _stable_encoding_value(different)
    assert signature != _stable_encoding_value(base(first))
    assert signature != _stable_encoding_value(other)
    equal.marker = "2"
    assert signature != _stable_encoding_value(equal)


@pytest.mark.parametrize(
    "base, method, first, second, hidden",
    [
        (str, "__str__", "a", "b", "hidden"),
        (int, "__int__", 1, 2, 0),
        (float, "__float__", 1.5, 2.5, 0.0),
        (complex, "__complex__", 1j, 2j, 0j),
        (bytes, "__bytes__", b"a", b"b", b"hidden"),
        (bytearray, "__bytes__", b"a", b"b", b"hidden"),
    ],
)
def test_primitive_subclass_overridden_conversions_cannot_hide_payload(
    base, method, first, second, hidden
):
    subclass = type("HiddenPayload", (base,), {method: lambda self: hidden})
    assert _stable_encoding_value(subclass(first)) != _stable_encoding_value(subclass(second))


def test_stateless_primitive_subclass_preserves_payload_and_exact_primitives_keep_signatures():
    class StatelessString(str):
        __slots__ = ()

    assert _stable_encoding_value(StatelessString("a")) != _stable_encoding_value(
        StatelessString("b")
    )
    assert _stable_encoding_value(StatelessString("a")) != _stable_encoding_value("a")
    for value, expected in [
        (None, ("none",)),
        (True, ("bool", True)),
        (1, ("int", 1)),
        (1.5, ("float", "1.5")),
        (1j, ("complex", "1j")),
        ("a", ("str", "a")),
        (b"a", ("bytes", b"a")),
        (bytearray(b"a"), ("bytes", b"a")),
        (memoryview(b"a"), ("bytes", b"a")),
    ]:
        assert _stable_encoding_value(value) == expected


def test_primitive_subclass_cycles_slots_and_enums_remain_distinct():
    first, second = MarkerString("1"), MarkerString("1")
    first.link, second.link = first, second
    assert _stable_encoding_value(first) == _stable_encoding_value(second)
    slotted = SlottedMarkerString("1")
    original = _stable_encoding_value(slotted)
    slotted.extra = "different"
    assert original != _stable_encoding_value(slotted)

    class Number(IntEnum):
        ONE = 1

    assert _stable_encoding_value(Number.ONE) != _stable_encoding_value(1)
    assert _stable_encoding_value(True) != _stable_encoding_value(1)


def test_primitive_enum_signatures_include_inspectable_state():
    class Number(IntEnum):
        ONE = 1
        TWO = 2

    class Text(StrEnum):
        ONE = "one"

    assert _stable_encoding_value(Number.ONE) != _stable_encoding_value(Number.TWO)
    for member in (Number.ONE, Text.ONE):
        original = _stable_encoding_value(member)
        member.marker = "extra"
        assert original != _stable_encoding_value(member)


def test_primitive_subclass_compilation_and_assignment_are_hash_seed_independent():
    script = """
from tests.test_relaxng_assignment import (
    MarkerString, SlottedMarkerString, compile_primitive_payloads, serialized, typed,
)
from pyxsd.relaxng.projection import _stable_encoding_value
for payload_type in (MarkerString, SlottedMarkerString):
    print(_stable_encoding_value(payload_type("1")))
    try:
        compile_primitive_payloads((payload_type("1"), payload_type("2")))
    except ValueError as error:
        print(error)
    else:
        raise AssertionError("conflicting primitive subclass state compiled")
    schema = compile_primitive_payloads((payload_type("1"), payload_type("1")))
    for field in ("first", "second"):
        document = typed(schema, f'<doc><{field} v="seed"/></doc>')
        getattr(document.root, field).v = "value"
        print(serialized(document))
"""
    results = [
        subprocess.check_output(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        )
        for seed in ("1", "981")
    ]
    assert results[0] == results[1]
    assert '<doc><first v="1:value"/></doc>' in results[0]
    assert '<doc><second v="1:value"/></doc>' in results[0]


def test_frozen_slots_dataclass_marker_conflicts_fail_at_compile_time():
    messages = []
    for _ in range(2):
        with pytest.raises(ValueError, match="conflicting RELAX NG encoding patterns") as caught:
            compile_body(
                encoded_two_child_schema(SLOTTED, "slotted"),
                datatype_libraries={
                    SLOTTED: DatatypeLibraryRegistration(
                        SlottedMarkerFactory(("1", "2")), "slotted/1"
                    ),
                },
            )
        messages.append(str(caught.value))
    assert messages[0] == messages[1]


def test_equal_frozen_slots_dataclass_markers_compile_and_assign_deterministically():
    schema = compile_body(
        encoded_two_child_schema(SLOTTED, "slotted"),
        datatype_libraries={
            SLOTTED: DatatypeLibraryRegistration(SlottedMarkerFactory(("1", "1")), "slotted/1"),
        },
    )
    first = typed(schema, '<doc><first v="seed"/></doc>')
    second = typed(schema, '<doc><second v="seed"/></doc>')

    first.root.first.v = "value"
    second.root.second.v = "value"

    assert serialized(first) == '<doc><first v="slotted:1:value"/></doc>'
    assert serialized(second) == '<doc><second v="slotted:1:value"/></doc>'


@pytest.mark.parametrize(
    "library, type_name, factory, markers",
    [
        (INHERITED, "inherited", InheritedSlotFactory, ("1", "2")),
        (DUPLICATE, "duplicate", DuplicateSlotFactory, ("1", "2")),
        (ITERATOR, "iterator", IteratorSlotFactory, ("1", "2")),
        (CYCLE_SHAPES, "cycle-shapes", CycleShapeFactory, ("ancestor", "self")),
        (MAPPING_MARKER, "mapping-marker", MappingMarkerFactory, ("1", "2")),
        (NESTED_CONTAINER, "nested-container", NestedContainerFactory, ("1", "2")),
    ],
    ids=[
        "inherited-slot",
        "duplicate-slot",
        "iterator-slot",
        "cycle-shape",
        "mapping-subclass",
        "nested-container",
    ],
)
def test_differing_inspectable_encoder_state_is_rejected_at_compile_time(
    library, type_name, factory, markers
):
    with pytest.raises(
        ValueError, match=r"conflicting RELAX NG encoding patterns|scalar contract identity"
    ):
        compile_body(
            encoded_two_child_schema(library, type_name),
            datatype_libraries={
                library: DatatypeLibraryRegistration(factory(markers), "state/1"),
            },
        )


@pytest.mark.parametrize(
    "library, type_name, factory, markers, encoded",
    [
        (INHERITED, "inherited", InheritedSlotFactory, ("1", "1"), "inherited:1:fixed:value"),
        (DUPLICATE, "duplicate", DuplicateSlotFactory, ("1", "1"), "duplicate:1:value"),
        (ITERATOR, "iterator", IteratorSlotFactory, ("1", "1"), "iterator:1:value"),
        (
            CYCLE_SHAPES,
            "cycle-shapes",
            CycleShapeFactory,
            ("ancestor", "ancestor"),
            "cycle-shapes:root:value",
        ),
        (
            MAPPING_MARKER,
            "mapping-marker",
            MappingMarkerFactory,
            ("1", "1"),
            "mapping-marker:1:value",
        ),
        (
            NESTED_CONTAINER,
            "nested-container",
            NestedContainerFactory,
            ("1", "1"),
            "nested-container:1:value",
        ),
    ],
    ids=[
        "inherited-slot",
        "duplicate-slot",
        "iterator-slot",
        "cycle-shape",
        "mapping-subclass",
        "nested-container",
    ],
)
def test_equal_inspectable_encoder_state_compiles_and_assigns(
    library, type_name, factory, markers, encoded
):
    schema = compile_body(
        encoded_two_child_schema(library, type_name),
        datatype_libraries={
            library: DatatypeLibraryRegistration(factory(markers), "state/1"),
        },
    )
    for field in ("first", "second"):
        document = typed(schema, f'<doc><{field} v="seed"/></doc>')
        getattr(document.root, field).v = "value"
        assert serialized(document) == f'<doc><{field} v="{encoded}"/></doc>'


def test_nested_slotted_state_conflicts_fail_at_compile_time():
    with pytest.raises(ValueError, match="conflicting RELAX NG encoding patterns"):
        compile_body(
            encoded_two_child_schema(NESTED, "nested"),
            datatype_libraries={
                NESTED: DatatypeLibraryRegistration(NestedSlottedFactory(("1", "2")), "nested/1"),
            },
        )


def test_cyclic_encoding_state_is_represented_without_addresses():
    schema = compile_body(
        '<attribute name="v">' + data("cyclic", CYCLIC) + "</attribute><empty/>",
        datatype_libraries={
            CYCLIC: DatatypeLibraryRegistration(CyclicStateFactory(), "cyclic/1"),
        },
    )
    document = typed(schema, '<doc v="seed"/>')

    document.root.v = "value"

    assert serialized(document) == '<doc v="cyclic:1:value"/>'


def test_unrepresentable_encoding_state_fails_closed_at_compile_time():
    with pytest.raises(
        ValueError,
        match=r"cannot represent RELAX NG encoding state for scalar contract identity",
    ):
        compile_body(
            '<attribute name="v">'
            + data("unrepresentable", UNREPRESENTABLE)
            + "</attribute><empty/>",
            datatype_libraries={
                UNREPRESENTABLE: DatatypeLibraryRegistration(
                    UnrepresentableStateFactory(), "unrepresentable/1"
                ),
            },
        )


def test_encoding_state_signatures_cover_primitives_collections_and_slots():
    values = [
        None,
        True,
        3,
        1.5,
        2 + 3j,
        "text",
        b"bytes",
        bytearray(b"bytes"),
        memoryview(b"bytes"),
        int,
        MarkerEnum.ONE,
        [1, "two"],
        {"z": 1, 2: "two", b"k": b"v"},
        {3, 1, 2},
        frozenset({"a", "b"}),
        PrivateSlotState("private"),
        SkippedSlotState("skipped"),
        MissingSlotState(),
        PureSlotsState("pure"),
        object.__new__(PureSlotsState),
        object(),
    ]
    for value in values:
        signature = _stable_encoding_value(value)
        assert signature == _stable_encoding_value(value)

    assert _stable_encoding_value(PrivateSlotState("one")) != _stable_encoding_value(
        PrivateSlotState("two")
    )
    assert _stable_encoding_value(SkippedSlotState("one")) != _stable_encoding_value(
        SkippedSlotState("two")
    )
    assert _stable_encoding_value(PureSlotsState("one")) != _stable_encoding_value(
        PureSlotsState("two")
    )
    initialized_missing = MissingSlotState()
    initialized_missing.marker = "set"
    assert _stable_encoding_value(MissingSlotState()) != _stable_encoding_value(initialized_missing)

    extra = ExtraDictState("field")
    extra.other = "extra"
    other_extra = ExtraDictState("field")
    other_extra.other = "different"
    assert _stable_encoding_value(extra) != _stable_encoding_value(other_extra)

    assert _stable_encoding_value(IteratorSlotState("one")) != _stable_encoding_value(
        IteratorSlotState("two")
    )
    assert _stable_encoding_value(DuplicateSlotState("one", "fixed")) != _stable_encoding_value(
        DuplicateSlotState("two", "fixed")
    )
    assert _stable_encoding_value(cycle_shape("ancestor")) != _stable_encoding_value(
        cycle_shape("self")
    )
    assert _stable_encoding_value(MarkerDict("one")) != _stable_encoding_value(MarkerDict("two"))

    non_string_key = type("DictKeyHolder", (), {})()
    non_string_key.__dict__[1] = "bad"
    with pytest.raises(ValueError, match="attribute name is not a string"):
        _stable_encoding_value(non_string_key)

    with pytest.raises(ValueError, match="cannot represent encoding state"):
        _stable_encoding_value(NotImplemented)

    shared = PureSlotsState("shared")
    assert _stable_encoding_value([shared, shared]) == _stable_encoding_value(
        [PureSlotsState("shared"), PureSlotsState("shared")]
    )

    deep: object = "leaf"
    for _ in range(102):
        deep = [deep]
    with pytest.raises(ValueError, match="nesting is too deep"):
        _stable_encoding_value(deep)


def test_slot_descriptors_from_another_owner_are_skipped():
    class CopiedSlotBase:
        __slots__ = ("marker",)

    class CopiedSlotChild(CopiedSlotBase):
        copied = CopiedSlotBase.marker

    value = CopiedSlotChild()
    value.marker = "value"
    entries = _descriptor_encoding_state(value, (), 0)

    assert [entry[2] for entry in entries].count("marker") == 1
    assert "copied" not in {entry[2] for entry in entries}


def test_signature_sorting_handles_scalar_keys_and_rejects_opaque_values():
    assert _signature_sort_key(1) != _signature_sort_key(2)
    assert _signature_sort_key(1.5) != _signature_sort_key(2.5)
    assert _signature_sort_key(True) != _signature_sort_key(False)
    assert _signature_sort_key(b"a") != _signature_sort_key(b"b")
    assert _signature_sort_key(None) == _signature_sort_key(None)
    with pytest.raises(ValueError, match="unsupported value"):
        _signature_sort_key(object())


def test_pattern_encoding_signature_handles_shared_children():
    datatype = XsdDatatypeFactory().create("integer", (), DatatypeContext(None, {}))
    shared = Data(datatype)
    signature = _pattern_encoding_signature(Group(shared, shared))

    assert len(signature) == 1


def test_production_assignment_does_not_search_instance_schema_patterns():
    import pyxsd.relaxng.binding as binding_module
    import pyxsd.relaxng.values as values_module

    for module in (binding_module, values_module):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "_schema_patterns" not in source


def test_empty_and_zero_or_more_lists_use_schema_static_empty_plans():
    empty_schema = compile_body("<list><empty/></list>")
    empty_document = typed(empty_schema, "<doc></doc>")
    empty_document.root.typed_value = ()
    assert serialized(empty_document) == "<doc></doc>"

    optional_schema = compile_body("<list><zeroOrMore>" + data("integer") + "</zeroOrMore></list>")
    optional_document = typed(optional_schema, "<doc>1 2</doc>")
    assert optional_document.root.typed_value == [1, 2]
    optional_document.root.typed_value = [3]
    assert serialized(optional_document) == "<doc>3</doc>"


def test_named_definition_list_items_use_their_compiled_ref_target():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}"><start>'
            f'<element name="doc" datatypeLibrary="{XSD}">'
            '<list><oneOrMore><ref name="item"/></oneOrMore></list>'
            "</element></start>"
            f'<define name="item">{data("integer")}</define>'
            "</grammar>"
        )
    )
    schema.require_valid()
    document = typed(schema, "<doc>1 2</doc>")

    document.root.typed_value = [3, 4]

    assert serialized(document) == "<doc>3 4</doc>"


@pytest.mark.parametrize(
    "body, xml",
    [
        (
            "<list><oneOrMore><choice>"
            + data("integer")
            + data("string")
            + "</choice></oneOrMore></list>",
            "<doc>1 2</doc>",
        ),
        (
            "<list><group><oneOrMore><choice>"
            + data("integer")
            + data("string")
            + "</choice></oneOrMore>"
            + data("integer")
            + "</group></list>",
            "<doc>1 2</doc>",
        ),
        (
            "<list><group><oneOrMore>"
            + data("integer")
            + "</oneOrMore>"
            + data("integer")
            + "</group></list>",
            "<doc>1 2</doc>",
        ),
        ("<list><choice>" + data("integer") + data("string") + "</choice></list>", "<doc>1</doc>"),
        (
            "<list><choice><oneOrMore><choice>"
            + data("integer")
            + data("string")
            + "</choice></oneOrMore>"
            + data("integer")
            + "</choice></list>",
            "<doc>1 2</doc>",
        ),
    ],
    ids=["one-or-more", "group-none", "group-list", "choice", "choice-none"],
)
def test_non_plain_list_token_plans_fail_closed(body, xml):
    schema = compile_body(body)
    document = typed(schema, xml)

    pattern = root_list_pattern(schema, document)

    assert list_shape(pattern) == "interpretation"
    assert _list_token_plan(pattern) is None


def test_unexpected_list_content_has_no_token_plan():
    assert _combine_list_tokens(NotAllowed(), {}) is None


def test_raw_list_token_plans_cover_references_and_mirrored_empty_choices():
    datatype = XsdDatatypeFactory().create("integer", (), DatatypeContext(None, {}))
    item = Data(datatype)

    reference = Ref()
    reference.bind(item)
    reference_plan = _list_token_plan(List(reference))

    assert reference_plan is not None
    assert reference_plan.kind == "fixed"
    assert reference_plan.patterns == (item,)

    mirrored = List(Choice(OneOrMore(item), Empty()))
    mirrored_plan = _list_token_plan(mirrored)

    assert list_shape(mirrored) == "list"
    assert mirrored_plan is not None
    assert mirrored_plan.kind == "list"
    assert mirrored_plan.patterns == (item,)
