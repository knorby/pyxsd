"""Scalar contracts, stable candidate identities, and atomic alternatives.

The tests exercise only accepting Task 1 witnesses.  Scalar values are
lexical evidence interpreted through the compiled datatype contracts; no
candidate is inferred from local datatype acceptance, object identity, or a
process-randomized digest.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from pyxsd.namespaces import NamespaceContext
from pyxsd.relaxng.contracts import (
    LIST_LIBRARY,
    LIST_TYPE_NAME,
    ScalarContract,
    ScalarRestriction,
    _pattern_identity_and_work,
    candidate_id,
    grammar_fingerprint,
    pattern_identity,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    BuiltinDatatypeFactory,
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeParameter,
    UnsupportedDatatypeError,
    UnsupportedDatatypeOperation,
    XsdDatatypeFactory,
    resolve_datatype_selector,
)
from pyxsd.relaxng.errors import AmbiguousValueError
from pyxsd.relaxng.evidence import build_binding_evidence
from pyxsd.relaxng.names import (
    AnyName,
    ExceptName,
    Name,
    NameChoice,
    NsName,
)
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element
from pyxsd.relaxng.values import (
    AlternativeValue,
    Candidate,
    alternative_value_from_witnesses,
    candidate_for_witness,
)

CX = DatatypeContext(None, {})
BUILTIN = BuiltinDatatypeFactory()
XSD = XsdDatatypeFactory()
STRING = BUILTIN.create("string", (), CX)
INTEGER = XSD.create("integer", (), CX)
BOOLEAN = XSD.create("boolean", (), CX)
QNAME = XSD.create("QName", (), CX)
RNG_NS = "http://relaxng.org/ns/structure/1.0"
XSD_LIB = "http://www.w3.org/2001/XMLSchema-datatypes"


def element(name, content=None):
    return Element(Name(None, name), Empty() if content is None else content)


def tree(xml):
    return RelaxNGElement.from_etree(ET.fromstring(xml))


def evidence(root, start, *, datatype_context=CX):
    assert validate_element(root, start).valid
    return build_binding_evidence(root, start, datatype_context=datatype_context, limits=None)


def site(graph, node, *, kind="text", token_index=None):
    matches = [
        occurrence
        for occurrence in graph.occurrences
        if occurrence.element is node
        and occurrence.kind == kind
        and occurrence.token_index == token_index
    ]
    assert len(matches) == 1
    return matches[0]


def witnesses_for(xml, content, *, datatype_context=CX):
    root = tree(xml)
    start = element("r", content)
    graph = evidence(root, start, datatype_context=datatype_context)
    return graph, graph.scalar_witnesses_at(site(graph, root))


def atomic_contract(
    type_name="integer",
    *,
    library_uri=XSD_LIBRARY,
    parameters=(),
    restrictions=(),
    pattern="pattern",
):
    return ScalarContract(
        "atomic",
        library_uri,
        type_name,
        tuple(parameters),
        tuple(restrictions),
        pattern,
    )


def candidate(identifier, contract=None, decoded=1, decodable=True):
    if contract is None:
        contract = atomic_contract(pattern=f"pattern-{identifier}")
    return Candidate(identifier, contract, decoded, decodable)


def wrapper(candidates, *, lexical="12", context=CX, grammar="grammar", revision=1):
    return AlternativeValue(lexical, tuple(candidates), context, grammar, revision)


class OpaqueDatatype(Datatype):
    """A custom datatype with no stable public library/type metadata."""

    def __init__(self, config="opaque"):
        self.config = config

    def __repr__(self):
        return f"OpaqueDatatype(config={self.config!r})"

    def allows(self, lexical, context):
        return lexical == "ok"

    def equal(self, left, left_context, right, right_context):
        return left == right


class OpaqueFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return OpaqueDatatype()


class ConfiguredOpaqueDatatype(Datatype):
    """Same class, different configuration, still without stable metadata."""

    def __init__(self, config):
        self.config = config

    def __repr__(self):
        return f"ConfiguredOpaqueDatatype(config={self.config!r})"

    def allows(self, lexical, context):
        return lexical == "ok"

    def equal(self, left, left_context, right, right_context):
        return left == right


class NonDecodingDatatype(Datatype):
    """A named custom datatype that validates but provides no decoder."""

    library_uri = "urn:custom"
    type_name = "opaque"
    parameters = ()

    def __repr__(self):
        return f"{type(self).__name__}()"

    def allows(self, lexical, context):
        return lexical == "ok"

    def equal(self, left, left_context, right, right_context):
        return left == right


class NamedOpaqueDatatype(NonDecodingDatatype):
    """A named custom datatype suitable for grammar identity tests."""

    type_name = "named"


class NamedOpaqueFactory(DatatypeFactory):
    """A factory whose datatypes expose stable metadata for identity."""

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return NamedOpaqueDatatype()


class ExplodingDecodeDatatype(Datatype):
    """A named custom datatype whose decoder fails for a reason other than capability."""

    library_uri = "urn:custom"
    type_name = "exploding"
    parameters = ()

    def __repr__(self):
        return f"{type(self).__name__}()"

    def allows(self, lexical, context):
        return lexical == "ok"

    def equal(self, left, left_context, right, right_context):
        return left == right

    def decode(self, lexical, context):
        raise RuntimeError("decoder exploded")


class NamedCustomDatatype(NonDecodingDatatype):
    type_name = "named"


class OtherNamedCustomDatatype(NonDecodingDatatype):
    type_name = "other"


class MalformedParametersDatatype(NonDecodingDatatype):
    parameters: Any = None


class MalformedWhiteSpaceDatatype(NonDecodingDatatype):
    white_space = 3


class MalformedContextDatatype(NonDecodingDatatype):
    semantic_context = (("p",),)


class NonIterableContextDatatype(NonDecodingDatatype):
    semantic_context = None


class StringContextDatatype(NonDecodingDatatype):
    semantic_context = ("p",)


class NonStringContextDatatype(NonDecodingDatatype):
    semantic_context = ((1, "urn:p"),)


def compile_schema(xml, *, datatype_libraries=None):
    schema = RelaxNGSchema.compile(io.StringIO(xml), datatype_libraries=datatype_libraries)
    assert not schema.report.has_errors, [issue.message for issue in schema.report.issues]
    return schema


def registration(fingerprint):
    return DatatypeLibraryRegistration(OpaqueFactory(), fingerprint)


def named_registration(fingerprint):
    return DatatypeLibraryRegistration(NamedOpaqueFactory(), fingerprint)


SCHEMA = (
    f'<element xmlns="{RNG_NS}" name="r"><choice>'
    f'<data type="integer" datatypeLibrary="{XSD_LIB}"/>'
    '<data type="string"/>'
    "</choice></element>"
)
OTHER_SCHEMA = f'<element xmlns="{RNG_NS}" name="r"><data type="integer" datatypeLibrary="{XSD_LIB}"/></element>'
RECURSIVE_SCHEMA = (
    f'<grammar xmlns="{RNG_NS}"><start>'
    '<element name="r"><ref name="item"/></element></start>'
    '<define name="item"><element name="item"><optional><ref name="item"/></optional></element></define>'
    "</grammar>"
)
CUSTOM_SCHEMA = (
    f'<element xmlns="{RNG_NS}" name="r">'
    '<data type="opaque" datatypeLibrary="urn:custom"/></element>'
)


def doubling_schema(depth):
    """A definition chain where each level references the previous one twice."""
    definitions = ['<define name="d0"><element name="n"><empty/></element></define>']
    for index in range(1, depth + 1):
        definitions.append(
            f'<define name="d{index}"><element name="n"><group>'
            f'<ref name="d{index - 1}"/><ref name="d{index - 1}"/>'
            "</group></element></define>"
        )
    return (
        f'<grammar xmlns="{RNG_NS}"><start>'
        f'<element name="root"><ref name="d{depth}"/></element>'
        f"</start>{''.join(definitions)}</grammar>"
    )


def cyclic_sharing_root(*, shared_leaf, leaf_first=True):
    """A cyclic element plus a root that reuses its acyclic leaf descendant."""
    leaf = Element(Name(None, "leaf"), Empty())
    other_leaf = leaf if shared_leaf else Element(Name(None, "leaf"), Empty())
    self_reference = Ref()
    leaf_reference = Ref()
    leaf_reference.bind(leaf)
    content = Choice(leaf_reference, self_reference)
    if not leaf_first:
        content = Choice(self_reference, leaf_reference)
    branch = Element(Name(None, "b"), content)
    self_reference.bind(branch)
    branch_reference = Ref()
    branch_reference.bind(branch)
    first_leaf_reference = Ref()
    first_leaf_reference.bind(leaf)
    second_leaf_reference = Ref()
    second_leaf_reference.bind(other_leaf)
    return Group(
        branch_reference,
        Group(first_leaf_reference, second_leaf_reference),
    )


def equal_cyclic_roots(*, shared_branch):
    """Two references to one cyclic branch, or to equal separate branches."""
    first_branch = cyclic_sharing_root(shared_leaf=True)
    second_branch = first_branch if shared_branch else cyclic_sharing_root(shared_leaf=True)
    first_reference = Ref()
    first_reference.bind(first_branch)
    second_reference = Ref()
    second_reference.bind(second_branch)
    return Group(first_reference, second_reference)


class TestScalarContract:
    def test_contracts_are_frozen_hashable_and_deterministically_represented(self):
        first = scalar_contract(Data(INTEGER))
        second = scalar_contract(Data(INTEGER))

        assert first == second
        assert hash(first) == hash(second)
        assert repr(first) == repr(second)
        assert "0x" not in repr(first)
        assert first.kind == "atomic"
        assert (first.library_uri, first.type_name) == (XSD_LIBRARY, "integer")
        assert first.parameters == ()
        assert first.restrictions == ()
        with pytest.raises(FrozenInstanceError):
            first.kind = "text"  # type: ignore[misc]

        text = scalar_contract(Text())
        assert text.kind == "text"
        assert text.library_uri is None
        assert text.type_name is None

        listed = scalar_contract(List(Data(INTEGER)))
        assert listed.kind == "list"
        assert (listed.library_uri, listed.type_name) == (LIST_LIBRARY, LIST_TYPE_NAME)

    def test_parameters_and_restrictions_are_normalized_immutable_values(self):
        later = DatatypeParameter("maxInclusive", "10")
        earlier = DatatypeParameter("minInclusive", "0")
        first = ScalarRestriction("except", "excluded-one")
        second = ScalarRestriction("except", "excluded-two")
        forward = ScalarContract(
            "atomic", XSD_LIBRARY, "integer", (later, earlier), (second, first), "pattern"
        )
        backward = ScalarContract(
            "atomic", XSD_LIBRARY, "integer", (earlier, later), (first, second, first), "pattern"
        )

        assert forward == backward
        assert hash(forward) == hash(backward)
        assert forward.parameters == (
            DatatypeParameter("maxInclusive", "10"),
            DatatypeParameter("minInclusive", "0"),
        )
        assert forward.restrictions == (first, second)
        assert all(isinstance(parameter, DatatypeParameter) for parameter in forward.parameters)
        assert all(isinstance(item, ScalarRestriction) for item in forward.restrictions)

    @pytest.mark.parametrize(
        "contract_args, error",
        [
            (("other", "", "integer", (), (), "pattern"), ValueError),
            (("text", XSD_LIBRARY, "string", (), (), "pattern"), ValueError),
            (
                (
                    "text",
                    None,
                    None,
                    (DatatypeParameter("minInclusive", "0"),),
                    (),
                    "pattern",
                ),
                ValueError,
            ),
            (("text", None, None, (), (ScalarRestriction("value", "p"),), "pattern"), ValueError),
            (("atomic", None, "integer", (), (), "pattern"), ValueError),
            (("atomic", XSD_LIBRARY, None, (), (), "pattern"), ValueError),
            (("atomic", XSD_LIBRARY, "", (), (), "pattern"), ValueError),
            (("atomic", XSD_LIBRARY, "integer", (), (), ""), ValueError),
            (("atomic", XSD_LIBRARY, "integer", (), (), None), ValueError),
            (("atomic", XSD_LIBRARY, "integer", (object(),), (), "pattern"), TypeError),
            (("atomic", XSD_LIBRARY, "integer", (), (object(),), "pattern"), TypeError),
        ],
    )
    def test_invalid_contracts_are_rejected(self, contract_args, error):
        with pytest.raises(error):
            ScalarContract(*contract_args)

    @pytest.mark.parametrize(
        "kind, identity, error",
        [("value", "", ValueError), ("other", "pattern", ValueError), ("value", None, ValueError)],
    )
    def test_invalid_restrictions_are_rejected(self, kind, identity, error):
        with pytest.raises(error):
            ScalarRestriction(kind, identity)

    def test_value_and_data_except_contracts_record_their_restrictions(self):
        value = scalar_contract(Value(INTEGER, "0"))
        other_value = scalar_contract(Value(INTEGER, "1"))
        excluded = scalar_contract(DataExcept(INTEGER, Value(INTEGER, "0")))

        assert value.restrictions == (
            ScalarRestriction("value", pattern_identity(Value(INTEGER, "0"))),
        )
        assert value.pattern_identity != other_value.pattern_identity
        assert excluded.restrictions[0].kind == "except"
        assert excluded.restrictions[0].pattern_identity == pattern_identity(Value(INTEGER, "0"))

    def test_opaque_custom_datatypes_cannot_fabricate_metadata(self):
        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            scalar_contract(Data(OpaqueDatatype()))
        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            pattern_identity(Data(OpaqueDatatype("configured")))

    @pytest.mark.parametrize(
        "datatype, message",
        [
            (MalformedParametersDatatype(), "parameters"),
            (MalformedWhiteSpaceDatatype(), "whitespace"),
            (MalformedContextDatatype(), "namespace context"),
            (NonIterableContextDatatype(), "namespace context"),
            (StringContextDatatype(), "namespace context"),
            (NonStringContextDatatype(), "namespace context"),
        ],
    )
    def test_malformed_custom_metadata_is_an_explicit_capability_failure(self, datatype, message):
        with pytest.raises(UnsupportedDatatypeOperation, match=message):
            scalar_contract(Data(datatype))


class TestPatternIdentity:
    def test_equal_semantic_patterns_share_an_identity_across_instances(self):
        assert pattern_identity(Data(INTEGER)) == pattern_identity(Data(INTEGER))
        assert pattern_identity(Text()) == pattern_identity(Text())
        assert pattern_identity(Data(INTEGER)) != pattern_identity(Data(STRING))

    def test_every_structural_pattern_and_name_class_has_a_stable_encoding(self):
        name = Name(None, "a")
        patterns = [
            Empty(),
            NotAllowed(),
            Text(),
            Data(INTEGER),
            Value(INTEGER, "0"),
            DataExcept(INTEGER, Value(INTEGER, "0")),
            List(Data(INTEGER)),
            OneOrMore(Data(INTEGER)),
            Group(Data(INTEGER), Text()),
            Interleave(Data(INTEGER), Text()),
            Choice(Data(INTEGER), Data(STRING)),
            Element(name, Empty()),
            Attribute(name, Empty()),
            Element(Name(None, "r"), Attribute(name, Empty())),
        ]
        identities = [pattern_identity(item) for item in patterns]
        assert len(set(identities)) == len(identities)
        assert identities == [pattern_identity(item) for item in patterns]

        names = [
            Name(None, "a"),
            Name("urn:a", "a"),
            AnyName(),
            NsName(None),
            NsName("urn:a"),
            NameChoice((Name(None, "a"), Name(None, "b"))),
            ExceptName(AnyName(), Name(None, "a")),
        ]
        name_identities = {pattern_identity(Element(value, Empty())) for value in names}
        assert len(name_identities) == len(names)

    def test_sharing_does_not_change_an_acyclic_pattern_identity(self):
        shared = Data(INTEGER)
        first = Group(shared, shared)
        second = Group(Data(INTEGER), Data(INTEGER))
        assert pattern_identity(first) == pattern_identity(second)

    def test_ref_cycles_are_encoded_deterministically(self):
        def recursive():
            reference = Ref()
            target = Element(Name(None, "item"), Choice(reference, Empty()))
            reference.bind(target)
            return target

        first = recursive()
        second = recursive()
        assert pattern_identity(first) == pattern_identity(second)
        assert pattern_identity(first) != pattern_identity(
            Element(Name(None, "item"), Choice(Element(Name(None, "leaf"), Empty()), Empty()))
        )

    def test_unbound_references_and_non_patterns_are_explicit_errors(self):
        with pytest.raises(ValueError, match="unbound"):
            pattern_identity(Ref())
        with pytest.raises(TypeError, match="unknown"):
            pattern_identity(object())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="name class"):
            pattern_identity(Element(object(), Empty()))  # type: ignore[arg-type]

    def test_reference_chain_cycles_are_explicit_errors(self):
        first = Ref()
        second = Ref()
        first.bind(second)
        second.bind(first)
        with pytest.raises(ValueError, match="reference chain"):
            pattern_identity(first)

    def test_illegal_non_reference_cycles_are_explicit_errors(self):
        cycle = Group(Empty(), Empty())
        object.__setattr__(cycle, "first", cycle)
        with pytest.raises(ValueError, match="outside a bound reference"):
            pattern_identity(cycle)

    def test_non_scalar_patterns_are_rejected(self):
        with pytest.raises(TypeError, match="not a RELAX NG scalar"):
            scalar_contract(Empty())

    def test_reference_targets_traverse_data_except_exclusions(self):
        target = DataExcept(INTEGER, Value(INTEGER, "0"))
        first = Ref()
        second = Ref()
        first.bind(target)
        second.bind(target)
        assert pattern_identity(first) == pattern_identity(second)

    def test_datatype_payload_structure_participates_in_identity(self):
        constrained = XSD.create("integer", (DatatypeParameter("maxInclusive", "10"),), CX)
        qname_p = XSD.create("QName", (), DatatypeContext(None, {"p": "urn:p"}))
        qname_q = XSD.create("QName", (), DatatypeContext(None, {"q": "urn:q"}))
        named = Data(NamedCustomDatatype())
        other_named = Data(OtherNamedCustomDatatype())

        assert pattern_identity(Data(INTEGER)) != pattern_identity(Data(constrained))
        assert pattern_identity(Data(qname_p)) != pattern_identity(Data(qname_q))
        assert pattern_identity(named) != pattern_identity(other_named)
        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            pattern_identity(Data(OpaqueDatatype()))
        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            pattern_identity(Data(OpaqueDatatype("other")))
        assert pattern_identity(Value(INTEGER, "0")) != pattern_identity(Value(INTEGER, "1"))
        assert pattern_identity(DataExcept(INTEGER, Value(INTEGER, "0"))) != pattern_identity(
            DataExcept(INTEGER, Value(INTEGER, "1"))
        )


class TestGrammarFingerprint:
    def test_recompiling_the_same_schema_produces_the_same_fingerprint(self):
        first = compile_schema(SCHEMA)
        second = compile_schema(SCHEMA)

        first_fingerprint = grammar_fingerprint(first.grammar, first.datatype_fingerprint)
        second_fingerprint = grammar_fingerprint(second.grammar, second.datatype_fingerprint)
        assert first_fingerprint == second_fingerprint
        assert pattern_identity(first.grammar.start) == pattern_identity(second.grammar.start)
        contract = atomic_contract()
        assert candidate_id(first_fingerprint, contract) == candidate_id(
            second_fingerprint, contract
        )

    def test_recursive_grammars_and_definitions_are_stable(self):
        first = compile_schema(RECURSIVE_SCHEMA)
        second = compile_schema(RECURSIVE_SCHEMA)

        assert first.grammar.definitions
        assert grammar_fingerprint(
            first.grammar, first.datatype_fingerprint
        ) == grammar_fingerprint(second.grammar, second.datatype_fingerprint)

    def test_structure_and_registry_identity_change_the_fingerprint(self):
        schema = compile_schema(SCHEMA)
        other = compile_schema(OTHER_SCHEMA)
        base = grammar_fingerprint(schema.grammar, schema.datatype_fingerprint)

        assert base != grammar_fingerprint(other.grammar, other.datatype_fingerprint)
        assert base != grammar_fingerprint(schema.grammar, "other-registry/1")

    def test_custom_registration_fingerprints_participate(self):
        first = compile_schema(
            CUSTOM_SCHEMA,
            datatype_libraries={"urn:custom": named_registration("custom/1")},
        )
        second = compile_schema(
            CUSTOM_SCHEMA,
            datatype_libraries={"urn:custom": named_registration("custom/2")},
        )
        repeat = compile_schema(
            CUSTOM_SCHEMA,
            datatype_libraries={"urn:custom": named_registration("custom/1")},
        )

        first_id = grammar_fingerprint(first.grammar, first.datatype_fingerprint)
        second_id = grammar_fingerprint(second.grammar, second.datatype_fingerprint)
        assert first_id != second_id
        assert first_id == grammar_fingerprint(repeat.grammar, repeat.datatype_fingerprint)
        contract = atomic_contract()
        assert candidate_id(first_id, contract) != candidate_id(second_id, contract)

    def test_opaque_custom_datatype_grammar_identity_is_rejected_but_validation_is_not(self):
        schema = compile_schema(
            CUSTOM_SCHEMA,
            datatype_libraries={"urn:custom": registration("custom/1")},
        )
        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            grammar_fingerprint(schema.grammar, schema.datatype_fingerprint)

        document = schema.parse(io.StringIO("<r>ok</r>"), typed=False)
        assert document.is_valid

    def test_source_location_is_not_part_of_the_fingerprint(self, tmp_path):
        schema = compile_schema(SCHEMA)
        path = tmp_path / "schema.rng"
        path.write_text(SCHEMA, encoding="utf-8")
        from_path = RelaxNGSchema.compile(path)

        assert grammar_fingerprint(schema.grammar, schema.datatype_fingerprint) == (
            grammar_fingerprint(from_path.grammar, from_path.datatype_fingerprint)
        )
        assert str(tmp_path) not in grammar_fingerprint(
            from_path.grammar, from_path.datatype_fingerprint
        )

    def test_invalid_registry_fingerprints_are_rejected(self):
        schema = compile_schema(SCHEMA)
        with pytest.raises(TypeError, match="CompiledGrammar"):
            grammar_fingerprint(object(), schema.datatype_fingerprint)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="fingerprint"):
            grammar_fingerprint(schema.grammar, None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="fingerprint"):
            grammar_fingerprint(schema.grammar, "")


class TestBoundedIdentityWork:
    def test_cyclic_root_does_not_poison_shared_acyclic_descendants(self):
        for leaf_first in (True, False):
            shared = pattern_identity(cyclic_sharing_root(shared_leaf=True, leaf_first=leaf_first))
            distinct = pattern_identity(
                cyclic_sharing_root(shared_leaf=False, leaf_first=leaf_first)
            )
            assert shared == distinct

    def test_cyclic_descendant_sharing_is_process_independent(self):
        program = """
from tests.test_relaxng_values import cyclic_sharing_root, equal_cyclic_roots
from pyxsd.relaxng.contracts import pattern_identity
for leaf_first in (True, False):
    print(pattern_identity(cyclic_sharing_root(shared_leaf=True, leaf_first=leaf_first)))
    print(pattern_identity(cyclic_sharing_root(shared_leaf=False, leaf_first=leaf_first)))
print(pattern_identity(equal_cyclic_roots(shared_branch=True)))
print(pattern_identity(equal_cyclic_roots(shared_branch=False)))
"""
        outputs = [
            subprocess.run(
                [sys.executable, "-c", program],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            for seed in ("1", "42")
        ]
        assert outputs[0] == outputs[1]
        lines = outputs[0].splitlines()
        assert len(lines) == 6
        assert lines[0] == lines[1]
        assert lines[2] == lines[3]
        assert lines[4] != lines[5]

    def test_equal_but_distinct_cyclic_targets_do_not_share_an_expansion(self):
        shared = pattern_identity(equal_cyclic_roots(shared_branch=True))
        distinct = pattern_identity(equal_cyclic_roots(shared_branch=False))
        assert shared != distinct
        assert distinct == pattern_identity(equal_cyclic_roots(shared_branch=False))

    def test_doubling_references_are_encoded_once_and_recompile_stably(self):
        xml = doubling_schema(30)
        first = compile_schema(xml)
        second = compile_schema(xml)

        first_identity, work = _pattern_identity_and_work(first.grammar.start)
        second_identity, second_work = _pattern_identity_and_work(second.grammar.start)
        assert first_identity == second_identity
        assert work == second_work
        assert work < 1000, work
        assert grammar_fingerprint(first.grammar, first.datatype_fingerprint) == (
            grammar_fingerprint(second.grammar, second.datatype_fingerprint)
        )

    def test_doubling_ids_are_process_independent(self):
        xml = doubling_schema(30)
        program = """
import io
import sys
from pyxsd.relaxng.contracts import grammar_fingerprint, pattern_identity
from pyxsd.relaxng.schema import RelaxNGSchema
schema = RelaxNGSchema.compile(io.StringIO(sys.argv[1]))
print(grammar_fingerprint(schema.grammar, schema.datatype_fingerprint))
print(pattern_identity(schema.grammar.start))
"""
        outputs = [
            subprocess.run(
                [sys.executable, "-c", program, xml],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            for seed in ("1", "42")
        ]
        assert outputs[0] == outputs[1]


class TestCandidateIdentity:
    def test_candidate_ids_are_stable_and_sensitive_to_every_identity_component(self):
        contract = atomic_contract()
        identifier = candidate_id("grammar", contract)

        assert identifier == candidate_id("grammar", atomic_contract())
        assert identifier != candidate_id("other-grammar", contract)
        assert identifier != candidate_id("grammar", atomic_contract(type_name="long"))
        assert identifier != candidate_id("grammar", atomic_contract(library_uri="urn:other"))
        assert identifier != candidate_id(
            "grammar",
            atomic_contract(parameters=(DatatypeParameter("maxInclusive", "10"),)),
        )
        assert identifier != candidate_id(
            "grammar",
            atomic_contract(restrictions=(ScalarRestriction("except", "excluded"),)),
        )
        assert identifier != candidate_id("grammar", atomic_contract(pattern="other-pattern"))
        assert identifier.startswith("pyxsd.relaxng.candidate/1:")
        assert "0x" not in identifier
        assert identifier == identifier.lower()
        assert identifier == (
            "pyxsd.relaxng.candidate/1:"
            "a9f9a2825672ecd3feec229508457b637c763061910f01f103f5c889ce378d82"
        )
        assert pattern_identity(Data(INTEGER)) == (
            "pyxsd.relaxng.pattern/1:"
            "b402b026b7f80f16719dda67f3b61fa5035393d0bc10b8f832762cb004bb4b2b"
        )

    def test_candidate_id_rejects_invalid_inputs(self):
        contract = atomic_contract()
        with pytest.raises(TypeError, match="fingerprint"):
            candidate_id(None, contract)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="fingerprint"):
            candidate_id("", contract)
        with pytest.raises(TypeError, match="ScalarContract"):
            candidate_id("grammar", object())  # type: ignore[arg-type]

    def test_builtin_schema_candidate_id_is_preserved(self):
        schema = compile_schema(OTHER_SCHEMA)
        fingerprint = grammar_fingerprint(schema.grammar, schema.datatype_fingerprint)
        contract = scalar_contract(Data(INTEGER))
        assert candidate_id(fingerprint, contract) == (
            "pyxsd.relaxng.candidate/1:"
            "dac777a231f900f1626adc6b5a2c124ae68b2b2625664dd58cda7fbdf361ccab"
        )

    def test_ids_do_not_change_with_instance_values(self):
        data = Data(INTEGER)
        contract = scalar_contract(data)
        first = candidate_id("grammar", contract)
        second = candidate_id("grammar", scalar_contract(Data(INTEGER)))
        assert first == second

        _, witnesses = witnesses_for("<r>12</r>", Data(INTEGER))
        (witness,) = witnesses
        assert candidate_for_witness(witness, grammar_fingerprint="grammar").decoded == 12
        _, witnesses = witnesses_for("<r>13</r>", Data(INTEGER))
        (witness,) = witnesses
        assert candidate_for_witness(witness, grammar_fingerprint="grammar").candidate_id == first

    def test_ids_are_process_independent(self):
        program = f"""
import io
from pyxsd.relaxng.contracts import ScalarContract, candidate_id, grammar_fingerprint, pattern_identity
from pyxsd.relaxng.schema import RelaxNGSchema
schema = RelaxNGSchema.compile(io.StringIO({SCHEMA!r}))
fingerprint = grammar_fingerprint(schema.grammar, schema.datatype_fingerprint)
print(fingerprint)
contract = ScalarContract('atomic', {XSD_LIBRARY!r}, 'integer', (), (), 'pattern')
print(candidate_id(fingerprint, contract))
print(pattern_identity(schema.grammar.start))
recursive = RelaxNGSchema.compile(io.StringIO({RECURSIVE_SCHEMA!r}))
print(grammar_fingerprint(recursive.grammar, recursive.datatype_fingerprint))
print(pattern_identity(recursive.grammar.start))
"""
        outputs = [
            subprocess.run(
                [sys.executable, "-c", program],
                env={**os.environ, "PYTHONHASHSEED": seed},
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            for seed in ("1", "42")
        ]
        assert outputs[0] == outputs[1]


class TestCandidateAndAlternativeConstruction:
    def test_candidates_are_ordered_by_id_and_value_comparable(self):
        first = candidate("a")
        second = candidate("b")
        assert sorted([second, first]) == [first, second]
        assert first == candidate("a")
        assert first != candidate("b")
        assert first != candidate("a", decoded=2)
        assert first != candidate("a", decodable=False, decoded=None)
        assert first != "a"
        assert Candidate.__lt__(first, object()) is NotImplemented
        with pytest.raises(FrozenInstanceError):
            first.candidate_id = "b"
        with pytest.raises(TypeError, match="unhashable"):
            hash(first)

    @pytest.mark.parametrize(
        "args, error",
        [
            (("", atomic_contract(), 1, True), ValueError),
            ((None, atomic_contract(), 1, True), ValueError),
            (("a", object(), 1, True), TypeError),
            (("a", atomic_contract(), 1, "yes"), TypeError),
            (("a", atomic_contract(), 1, False), ValueError),
            (("a", atomic_contract(), None, True), ValueError),
            (("a", scalar_contract(Text()), "words", True), ValueError),
        ],
    )
    def test_invalid_candidates_are_rejected(self, args, error):
        with pytest.raises(error):
            Candidate(*args)

    def test_text_candidates_are_never_decodable(self):
        text = scalar_contract(Text())
        candidate = Candidate("a", text, None, False)
        assert candidate.decoded is None
        assert not candidate.decodable
        with pytest.raises(ValueError, match="lexical"):
            Candidate("a", text, "words", True)

    def test_alternative_rejects_equal_contracts_with_different_manual_ids(self):
        with pytest.raises(ValueError, match="contracts"):
            wrapper(
                (
                    candidate("a", contract=atomic_contract()),
                    candidate("b", contract=atomic_contract()),
                )
            )

    def test_reprs_are_stable_and_avoid_decoded_object_representations(self):
        class Decoded:
            pass

        decoded = Decoded()
        item = Candidate("a", atomic_contract(), decoded, True)
        value = wrapper((item,))
        assert repr(item) == repr(Candidate("a", atomic_contract(), decoded, True))
        assert repr(decoded) not in repr(item)
        assert repr(value) == repr(wrapper((candidate("a"),)))
        assert repr(value).startswith("AlternativeValue(lexical=")
        assert "'a'" in repr(value)

    def test_alternatives_reject_zero_or_duplicate_candidates(self):
        with pytest.raises(ValueError, match="zero"):
            wrapper(())
        with pytest.raises(TypeError, match="Candidate"):
            AlternativeValue("12", (object(),), CX, "grammar", 1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="unique"):
            wrapper((candidate("a"), candidate("a", decoded=2)))

    @pytest.mark.parametrize(
        "lexical, context, grammar, revision, error",
        [
            (12, CX, "grammar", 1, TypeError),
            ("12", object(), "grammar", 1, TypeError),
            ("12", CX, "", 1, ValueError),
            ("12", CX, "grammar", True, ValueError),
            ("12", CX, "grammar", "1", ValueError),
        ],
    )
    def test_invalid_alternatives_are_rejected(self, lexical, context, grammar, revision, error):
        with pytest.raises(error):
            AlternativeValue(
                lexical,
                (candidate("a"),),
                context,
                grammar,
                revision,
            )

    def test_alternatives_are_immutable_unhashable_and_not_raw_scalars(self):
        value = wrapper((candidate("b"), candidate("a")))
        same = wrapper((candidate("a"), candidate("b")))
        assert value == same
        assert value != wrapper((candidate("a", decoded=2), candidate("b")))
        assert value != "12"
        assert value != 12
        with pytest.raises(TypeError, match="unhashable"):
            hash(value)
        with pytest.raises(FrozenInstanceError):
            value.lexical = "13"
        assert [item.candidate_id for item in value.candidates] == ["a", "b"]

    def test_context_and_candidates_are_copied_into_the_snapshot(self):
        context = DatatypeContext("urn:base", {"p": "urn:p"})
        candidates = [candidate("a")]
        value = wrapper(candidates, context=context)
        candidates.clear()

        assert value.context == context
        assert value.context is not context
        assert dict(value.context.namespaces) == {"p": "urn:p"}
        assert [item.candidate_id for item in value.candidates] == ["a"]


class TestAtomicAlternatives:
    def test_ambiguous_atomic_witnesses_decode_only_by_contract(self):
        _, witnesses = witnesses_for("<r>12</r>", Choice(Data(INTEGER), Data(STRING)))
        assert len(witnesses) == 2
        value = alternative_value_from_witnesses(
            witnesses, grammar_fingerprint="grammar", revision=7
        )

        assert value.lexical == "12"
        assert value.grammar_fingerprint == "grammar"
        assert value.revision == 7
        assert value.context == CX
        assert str(value) == "12"
        assert [item.candidate_id for item in value.candidates] == sorted(
            item.candidate_id for item in value.candidates
        )
        assert value.candidates[0].contract.kind == "atomic"

        assert value.as_type("xsd:integer") == 12
        assert value.as_type("string") == "12"
        assert value.as_type((XSD_LIBRARY, "integer")) == 12
        with pytest.raises(AmbiguousValueError, match="as_candidate"):
            value.as_python()
        with pytest.raises(ValueError, match="no surviving"):
            value.as_type("xsd:boolean")
        with pytest.raises(ValueError, match="no candidate"):
            value.as_candidate("missing")

        integer = next(item for item in value.candidates if item.contract.type_name == "integer")
        string = next(item for item in value.candidates if item.contract.type_name == "string")
        assert value.as_candidate(integer.candidate_id) == 12
        assert value.as_candidate(string.candidate_id) == "12"

    def test_one_surviving_contract_in_a_schema_static_wrapper_decodes_uniquely(self):
        _, witnesses = witnesses_for("<r>12</r>", Choice(Data(INTEGER), Data(BOOLEAN)))
        assert len(witnesses) == 1
        value = alternative_value_from_witnesses(
            witnesses, grammar_fingerprint="grammar", revision=1
        )

        assert len(value.candidates) == 1
        assert value.as_python() == 12
        assert value.as_type("xsd:integer") == 12

    def test_a_single_nondecodable_interpretation_stays_lexical(self):
        root = tree("<r>ok</r>")
        start = element("r", Data(NonDecodingDatatype()))
        graph = evidence(root, start)
        (witness,) = graph.scalar_witnesses_at(site(graph, root))
        value = alternative_value_from_witnesses(
            (witness,), grammar_fingerprint="grammar", revision=3
        )

        (item,) = value.candidates
        assert item.contract.kind == "atomic"
        assert item.contract.library_uri == "urn:custom"
        assert item.contract.type_name == "opaque"
        assert item.decoded is None
        assert item.decodable is False
        assert str(value) == "ok"
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_python()
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_type(("urn:custom", "opaque"))
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_candidate(item.candidate_id)

    def test_decode_failures_other_than_capability_propagate(self):
        root = tree("<r>ok</r>")
        start = element("r", Data(ExplodingDecodeDatatype()))
        graph = evidence(root, start)
        (witness,) = graph.scalar_witnesses_at(site(graph, root))

        with pytest.raises(TypeError, match="ScalarWitness"):
            candidate_for_witness(object(), grammar_fingerprint="grammar")  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="decoder exploded"):
            candidate_for_witness(witness, grammar_fingerprint="grammar")

    def test_same_type_with_distinct_contracts_requires_a_candidate(self):
        first = atomic_contract(pattern="first")
        second = atomic_contract(pattern="second")
        value = wrapper(
            (
                Candidate("a", first, 12, True),
                Candidate("b", second, 12, True),
            )
        )

        with pytest.raises(AmbiguousValueError, match="as_candidate"):
            value.as_type("xsd:integer")
        assert value.as_candidate("a") == 12
        assert value.as_candidate("b") == 12

    def test_text_is_a_distinct_lexical_contract_without_string_datatype_identity(self):
        _, witnesses = witnesses_for("<r>12</r>", Choice(Text(), Data(INTEGER)))
        value = alternative_value_from_witnesses(
            witnesses, grammar_fingerprint="grammar", revision=1
        )
        text = next(item for item in value.candidates if item.contract.kind == "text")

        assert text.contract.library_uri is None
        assert text.contract.type_name is None
        assert text.decoded is None
        assert text.decodable is False
        assert value.as_type("xsd:integer") == 12
        with pytest.raises(ValueError, match="no surviving"):
            value.as_type("string")
        with pytest.raises(AmbiguousValueError):
            value.as_python()
        assert str(value) == "12"

    def test_text_candidate_identity_ignores_the_lexical_content(self):
        _, empty_witnesses = witnesses_for("<r></r>", Text())
        _, word_witnesses = witnesses_for("<r>words</r>", Text())
        empty = alternative_value_from_witnesses(
            empty_witnesses, grammar_fingerprint="grammar", revision=1
        )
        words = alternative_value_from_witnesses(
            word_witnesses, grammar_fingerprint="grammar", revision=1
        )

        assert empty.lexical == ""
        assert words.lexical == "words"
        assert [item.candidate_id for item in empty.candidates] == [
            item.candidate_id for item in words.candidates
        ]
        assert [item.contract for item in empty.candidates] == [
            item.contract for item in words.candidates
        ]
        with pytest.raises(UnsupportedDatatypeOperation):
            empty.as_python()

    def test_qname_and_string_can_decode_equal_python_values_but_stay_distinct(self):
        etree = ET.fromstring("<r>p:x</r>")
        namespaces = NamespaceContext()
        namespaces.record(etree, {"p": "urn:p"})
        root = RelaxNGElement.from_etree(etree, namespaces=namespaces)
        start = element("r", Choice(Data(QNAME), Data(STRING)))
        graph = evidence(root, start, datatype_context=CX)
        witnesses = graph.scalar_witnesses_at(site(graph, root))
        value = alternative_value_from_witnesses(
            witnesses, grammar_fingerprint="grammar", revision=1
        )

        qname = value.as_type("xsd:QName")
        string = value.as_type("string")
        assert qname == string == "p:x"
        assert qname.to_expanded_name() == "{urn:p}x"
        assert len({item.contract for item in value.candidates}) == 2
        with pytest.raises(AmbiguousValueError):
            value.as_python()
        assert len({item.candidate_id for item in value.candidates}) == 2

    def test_lists_keep_a_whole_list_contract_but_need_list_decoding(self):
        _, witnesses = witnesses_for("<r>12</r>", List(Data(INTEGER)))
        value = alternative_value_from_witnesses(
            witnesses, grammar_fingerprint="grammar", revision=1
        )
        (item,) = value.candidates

        assert item.contract.kind == "list"
        assert item.contract.library_uri == LIST_LIBRARY
        assert item.contract.type_name == LIST_TYPE_NAME
        assert item.decoded is None
        assert item.decodable is False
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_python()
        with pytest.raises(ValueError, match="no surviving"):
            value.as_type("list")

    def test_distinct_opaque_configurations_in_exclusions_fail_instead_of_collapsing(self):
        first = Value(ConfiguredOpaqueDatatype("a"), "y")
        second = Value(ConfiguredOpaqueDatatype("b"), "y")
        start = element("r", Choice(DataExcept(INTEGER, first), DataExcept(INTEGER, second)))
        root = tree("<r>12</r>")
        graph = evidence(root, start)
        witnesses = graph.scalar_witnesses_at(site(graph, root))
        assert len(witnesses) == 2

        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            alternative_value_from_witnesses(witnesses, grammar_fingerprint="grammar", revision=1)

    def test_distinct_opaque_configurations_in_values_fail_instead_of_collapsing(self):
        first = Value(ConfiguredOpaqueDatatype("a"), "ok")
        second = Value(ConfiguredOpaqueDatatype("b"), "ok")
        start = element("r", Choice(first, second))
        root = tree("<r>ok</r>")
        graph = evidence(root, start)
        witnesses = graph.scalar_witnesses_at(site(graph, root))
        assert len(witnesses) == 2

        with pytest.raises(UnsupportedDatatypeOperation, match="stable"):
            alternative_value_from_witnesses(witnesses, grammar_fingerprint="grammar", revision=1)

    def test_duplicate_contracts_collapse_to_one_candidate(self):
        _, witnesses = witnesses_for("<r>12</r>", Data(INTEGER))
        (witness,) = witnesses
        value = alternative_value_from_witnesses(
            (witness, witness), grammar_fingerprint="grammar", revision=1
        )
        assert len(value.candidates) == 1

        other = alternative_value_from_witnesses(
            witnesses, grammar_fingerprint="grammar", revision=2
        )
        assert value.candidates == other.candidates

    def test_mixing_witness_sites_or_lexical_values_is_rejected(self):
        _, first = witnesses_for("<r>12</r>", Data(INTEGER))
        _, second = witnesses_for("<r>13</r>", Data(INTEGER))
        (witness,) = first
        other_context = type(witness)(
            witness.occurrence,
            witness.pattern,
            witness.lexical,
            DatatypeContext("urn:b", {}),
            witness.token_spans,
        )
        with pytest.raises(ValueError, match="one occurrence"):
            alternative_value_from_witnesses(
                (first[0], second[0]), grammar_fingerprint="grammar", revision=1
            )
        with pytest.raises(ValueError, match="context"):
            alternative_value_from_witnesses(
                (witness, other_context), grammar_fingerprint="grammar", revision=1
            )
        with pytest.raises(ValueError, match="witness"):
            alternative_value_from_witnesses(
                (object(),),  # type: ignore[arg-type]
                grammar_fingerprint="grammar",
                revision=1,
            )
        with pytest.raises(ValueError, match="witness"):
            alternative_value_from_witnesses(
                (witness, object()),  # type: ignore[arg-type]
                grammar_fingerprint="grammar",
                revision=1,
            )
        with pytest.raises(ValueError, match="witness"):
            alternative_value_from_witnesses((), grammar_fingerprint="grammar", revision=1)

    def test_mismatched_lexical_values_at_one_occurrence_are_rejected(self):
        _, witnesses = witnesses_for("<r>12</r>", Data(INTEGER))
        (witness,) = witnesses
        clone = type(witness)(
            witness.occurrence,
            witness.pattern,
            "13",
            witness.context,
            witness.token_spans,
        )
        with pytest.raises(ValueError, match="lexical"):
            alternative_value_from_witnesses(
                (witness, clone), grammar_fingerprint="grammar", revision=1
            )

    def test_xsd_shorthand_rejects_namespace_prefixed_selectors(self):
        value = wrapper((candidate("a"),))
        with pytest.raises(UnsupportedDatatypeError, match="xsd:"):
            value.as_type("p:integer")
        assert resolve_datatype_selector("xsd:integer") == (XSD_LIBRARY, "integer")
        assert resolve_datatype_selector("integer") == (BUILTIN_LIBRARY, "integer")
