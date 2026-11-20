"""Typed generic RELAX NG parsing, scalar access, and revision freshness.

Typed parsing keeps the generic :class:`RelaxNGElement` tree and adds complete
binding evidence for valid documents.  Scalar access is schema-static: the
wrapper shape follows the contracts a position can have in the grammar, not
which branch this particular lexical value made easiest.  Validation-only
parsing stays evidence-free, and every typed read is gated by the document's
validity and the current lexical revision.
"""

from __future__ import annotations

import io
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.relaxng import (
    AlternativeValue,
    ListInterpretation,
    RelaxNGDocument,
    RelaxNGLimitError,
    RelaxNGLimits,
    RelaxNGSchema,
    StaleInterpretationError,
)
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeParameter,
    UnsupportedDatatypeOperation,
)
from pyxsd.relaxng.evidence import EvidenceGraph, EvidenceOccurrence, build_binding_evidence
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.xml_writer import clone_relaxng

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
CUSTOM = "urn:custom"


def data(type_name: str, library: str = XSD) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{library}"/>'


def element(name: str, content: str = "<empty/>") -> str:
    return f'<element name="{name}">{content}</element>'


def compile_schema(body: str, **kwargs: object) -> RelaxNGSchema:
    source = f'<grammar xmlns="{NS}"><start>{body}</start></grammar>'
    compiled = RelaxNGSchema.compile(io.StringIO(source), **kwargs)  # type: ignore[arg-type]
    assert not compiled.report.has_errors, [issue.message for issue in compiled.report.issues]
    return compiled


def typed(compiled: RelaxNGSchema, xml: str | ET.Element, **kwargs: object):
    return compiled.parse(xml, typed=True, **kwargs)  # type: ignore[arg-type]


def validation_only(compiled: RelaxNGSchema, xml: str, **kwargs: object):
    return compiled.parse(io.StringIO(xml), typed=False, **kwargs)  # type: ignore[arg-type]


def scalar_schema() -> RelaxNGSchema:
    return compile_schema(element("doc", data("integer")))


def text_schema() -> RelaxNGSchema:
    return compile_schema(element("doc", "<text/>"))


def late_branch_schema() -> RelaxNGSchema:
    return compile_schema(
        '<element name="doc"><choice>'
        f'<group><element name="x">{data("integer")}</element>'
        '<element name="a"><empty/></element></group>'
        f'<group><element name="x">{data("string")}</element>'
        '<element name="b"><empty/></element></group>'
        "</choice></element>"
    )


def text_occurrence(document, node: RelaxNGElement):
    evidence = document.binding_evidence
    assert isinstance(evidence, EvidenceGraph)
    matches = [
        occurrence
        for occurrence in evidence.occurrences
        if occurrence.element is node
        and occurrence.kind == "text"
        and occurrence.content_index == 0
    ]
    assert len(matches) == 1
    return matches[0]


def attribute_occurrence(document, node: RelaxNGElement, expanded_name: str):
    evidence = document.binding_evidence
    assert isinstance(evidence, EvidenceGraph)
    matches = [
        occurrence
        for occurrence in evidence.occurrences
        if occurrence.element is node
        and occurrence.kind == "attribute"
        and occurrence.name == expanded_name
    ]
    assert len(matches) == 1
    return matches[0]


class OpaqueDatatype(Datatype):
    """A named custom datatype that validates but declares no decoder."""

    library_uri = CUSTOM
    type_name = "opaque"
    parameters = ()

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        return lexical == "ok"

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        return left == right


class OpaqueFactory(DatatypeFactory):
    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        del type_name, parameters, context
        return OpaqueDatatype()


OPAQUE_LIBRARIES = {
    CUSTOM: DatatypeLibraryRegistration(OpaqueFactory(), "opaque/1"),
}


@pytest.mark.parametrize(
    ("body", "xml", "expected_valid"),
    [
        (element("doc", data("integer")), "<doc>12</doc>", True),
        (element("doc", data("integer")), "<doc>words</doc>", False),
        (element("doc", "<text/>"), "<doc>hello</doc>", True),
        (element("doc", element("item")), "<doc><other/></doc>", False),
    ],
)
def test_typed_and_validation_only_verdicts_match(body, xml, expected_valid):
    compiled = compile_schema(body)

    typed_document = typed(compiled, io.StringIO(xml))
    validation_document = validation_only(compiled, xml)

    assert typed_document.typed is True
    assert validation_document.typed is False
    assert typed_document.is_valid is expected_valid
    assert typed_document.is_valid == validation_document.is_valid
    assert typed_document.report.error_count == validation_document.report.error_count
    assert [(issue.code, issue.severity, issue.message) for issue in typed_document.report] == [
        (issue.code, issue.severity, issue.message) for issue in validation_document.report
    ]
    if expected_valid:
        assert isinstance(typed_document.binding_evidence, EvidenceGraph)
        assert validation_document.binding_evidence is None
    else:
        assert typed_document.binding_evidence is None
        assert validation_document.binding_evidence is None


def test_typed_parse_uses_generated_projection_classes_for_stable_routes():
    compiled = scalar_schema()

    document = typed(compiled, io.StringIO("<doc>12</doc>"))

    assert document.typed is True
    generated = compiled.projection_classes.class_for(compiled.projection.roots[0])
    assert type(document.root) is generated
    assert issubclass(type(document.root), RelaxNGElement)
    assert not hasattr(compiled, "classes")
    assert not hasattr(document, "classes")


def test_unique_atomic_contract_returns_its_ordinary_python_value():
    document = typed(scalar_schema(), io.StringIO("<doc>12</doc>"))

    first = document.root.scalar_value()
    second = document.root.scalar_value()

    assert first == second == 12
    assert isinstance(first, int)


def test_non_bool_typed_option_is_rejected_before_input():
    with pytest.raises(TypeError, match="bool"):
        scalar_schema().parse(io.StringIO("<doc>12</doc>"), typed=1)  # type: ignore[arg-type]


def test_text_contract_stays_lexical_and_never_manufactures_xsd_string():
    text_document = typed(text_schema(), io.StringIO("<doc>hello</doc>"))

    value = text_document.root.scalar_value()

    assert isinstance(value, AlternativeValue)
    (candidate,) = value.candidates
    assert candidate.contract.kind == "text"
    assert candidate.contract.library_uri is None
    assert candidate.contract.type_name is None
    assert candidate.decodable is False
    assert candidate.decoded is None
    assert str(value) == "hello"
    with pytest.raises(UnsupportedDatatypeOperation):
        value.as_python()

    string_document = typed(
        compile_schema(element("doc", data("string"))), io.StringIO("<doc>hello</doc>")
    )
    string_value = string_document.root.scalar_value()
    assert string_value == "hello"
    assert not isinstance(string_value, AlternativeValue)


def test_ambiguous_atomic_contracts_wrap_and_select_explicitly():
    compiled = compile_schema(element("doc", f"<choice>{data('integer')}{data('string')}</choice>"))
    document = typed(compiled, io.StringIO("<doc>12</doc>"))

    value = document.root.scalar_value()

    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 2
    assert value.as_type("xsd:integer") == 12
    assert value.as_type("xsd:string") == "12"
    assert str(value) == "12"
    with pytest.raises(relaxng.AmbiguousValueError):
        value.as_python()


def test_late_pruned_contract_still_uses_the_schema_static_wrapper():
    compiled = late_branch_schema()

    for lexical in ("12", "words"):
        document = typed(compiled, io.StringIO(f"<doc><x>{lexical}</x><b/></doc>"))
        assert document.is_valid
        node = document.root.children[0]
        value = node.scalar_value()

        assert isinstance(value, AlternativeValue)
        (candidate,) = value.candidates
        assert candidate.contract.kind == "atomic"
        assert candidate.contract.library_uri == XSD
        assert candidate.contract.type_name == "string"
        assert candidate.decodable is True
        assert value.as_python() == lexical

        contracts = document.binding_evidence.schema_contracts_at(text_occurrence(document, node))
        assert {(contract.kind, contract.type_name) for contract in contracts} == {
            ("atomic", "integer"),
            ("atomic", "string"),
        }


def test_qname_values_keep_the_instance_namespace_context():
    compiled = compile_schema(element("doc", data("QName")))
    document = typed(compiled, io.StringIO('<doc xmlns:p="urn:p">p:x</doc>'))

    value = document.root.scalar_value()

    assert value == "p:x"
    assert value.to_expanded_name() == "{urn:p}x"


def test_late_pruned_candidate_identity_is_stable_across_instances():
    compiled = late_branch_schema()

    first = typed(compiled, io.StringIO("<doc><x>12</x><b/></doc>"))
    second = typed(compiled, io.StringIO("<doc><x>words</x><b/></doc>"))

    first_value = first.root.children[0].scalar_value()
    second_value = second.root.children[0].scalar_value()
    assert isinstance(first_value, AlternativeValue)
    assert isinstance(second_value, AlternativeValue)
    assert first_value.candidates[0].candidate_id == second_value.candidates[0].candidate_id
    assert first_value.interpretations == second_value.interpretations == ()


def test_candidate_ids_are_stable_across_equivalent_compilations():
    body = element("doc", f"<choice>{data('integer')}{data('string')}</choice>")

    first = typed(compile_schema(body), io.StringIO("<doc>12</doc>"))
    second = typed(compile_schema(body), io.StringIO("<doc>12</doc>"))

    first_value = first.root.scalar_value()
    second_value = second.root.scalar_value()
    assert isinstance(first_value, AlternativeValue)
    assert isinstance(second_value, AlternativeValue)
    assert tuple(candidate.candidate_id for candidate in first_value.candidates) == tuple(
        candidate.candidate_id for candidate in second_value.candidates
    )
    assert first_value.grammar_fingerprint == second_value.grammar_fingerprint


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ("<doc><x>12</x><a/></doc>", 12),
        ("<doc><x>12</x><b/></doc>", "12"),
    ],
)
def test_the_same_position_wraps_on_either_accepting_branch(xml, expected):
    document = typed(late_branch_schema(), io.StringIO(xml))

    value = document.root.children[0].scalar_value()

    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 1
    assert value.as_python() == expected


def test_single_nondecodable_atomic_contract_wraps_explicitly():
    compiled = compile_schema(
        element("doc", data("opaque", CUSTOM)),
        datatype_libraries=OPAQUE_LIBRARIES,
    )
    document = typed(compiled, io.StringIO("<doc>ok</doc>"))

    value = document.root.scalar_value()

    assert isinstance(value, AlternativeValue)
    (candidate,) = value.candidates
    assert candidate.contract.kind == "atomic"
    assert candidate.decodable is False
    assert candidate.decoded is None
    with pytest.raises(UnsupportedDatatypeOperation, match="decoder"):
        value.as_python()
    with pytest.raises(UnsupportedDatatypeOperation, match="decoder"):
        value.as_candidate(candidate.candidate_id)


def test_attribute_values_use_expanded_names_and_distinguish_absent_from_empty():
    compiled = compile_schema(
        '<element name="doc">'
        '<optional><attribute name="a"><text/></attribute></optional>'
        f'<attribute name="b">{data("integer")}</attribute>'
        '<optional><attribute name="c" ns="urn:attrs"><text/></attribute></optional>'
        '<optional><attribute name="flag"><empty/></attribute></optional>'
        "<empty/></element>"
    )

    absent_document = typed(compiled, io.StringIO('<doc xmlns:p="urn:attrs" b="12"/>'))
    root = absent_document.root
    assert root.attribute_value("a") is None
    assert root.attribute_value("b") == 12
    assert root.attribute_value("{urn:attrs}c") is None
    assert root.attribute_value("flag") is None
    assert root.attribute_lexical("b") == "12"

    empty_document = typed(
        compiled, io.StringIO('<doc xmlns:p="urn:attrs" b="12" a="" p:c="" flag=""/>')
    )
    empty_root = empty_document.root
    a_value = empty_root.attribute_value("a")
    c_value = empty_root.attribute_value("{urn:attrs}c")
    assert isinstance(a_value, AlternativeValue)
    assert isinstance(c_value, AlternativeValue)
    assert str(a_value) == ""
    assert str(c_value) == ""
    assert a_value is not None
    assert c_value is not None
    assert empty_root.attribute_lexical("flag") == ""
    assert empty_root.attribute_value("flag") is None


def test_ambiguous_attribute_contract_wraps_by_expanded_name():
    compiled = compile_schema(
        '<element name="doc"><attribute name="n" ns="urn:attrs">'
        f"<choice>{data('integer')}{data('string')}</choice>"
        "</attribute></element>"
    )
    document = typed(compiled, io.StringIO('<doc xmlns:p="urn:attrs" p:n="12"/>'))

    value = document.root.attribute_value("{urn:attrs}n")

    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 2
    assert value.as_type("xsd:integer") == 12
    assert value.as_type("xsd:string") == "12"
    identifiers = tuple(candidate.candidate_id for candidate in value.candidates)
    assert identifiers == tuple(sorted(set(identifiers)))

    again = typed(compiled, io.StringIO('<doc xmlns:p="urn:attrs" p:n="12"/>'))
    repeated = again.root.attribute_value("{urn:attrs}n")
    assert isinstance(repeated, AlternativeValue)
    assert tuple(candidate.candidate_id for candidate in repeated.candidates) == identifiers
    witness_site = attribute_occurrence(again, again.root, "{urn:attrs}n")
    assert all(
        witness.occurrence is witness_site
        for witness in again.binding_evidence.scalar_witnesses_at(witness_site)
    )


def test_unique_attribute_ignores_a_distinct_element_content_contract():
    compiled = compile_schema(
        '<element name="doc"><attribute name="z">'
        f"{data('integer')}</attribute>{data('string')}</element>"
    )
    document = typed(compiled, io.StringIO('<doc z="3">x</doc>'))

    value = document.root.attribute_value("z")
    assert value == 3
    assert not isinstance(value, AlternativeValue)

    attribute_site = attribute_occurrence(document, document.root, "z")
    contracts = document.binding_evidence.schema_contracts_at(attribute_site)
    assert [(contract.kind, contract.type_name) for contract in contracts] == [
        ("atomic", "integer")
    ]
    witnesses = document.binding_evidence.scalar_witnesses_at(attribute_site)
    assert len(witnesses) == 1
    assert witnesses[0].occurrence is attribute_site

    # The element text site keeps its own contract and decoded value.
    assert document.root.scalar_value() == "x"
    text_contracts = document.binding_evidence.schema_contracts_at(
        text_occurrence(document, document.root)
    )
    assert [(contract.kind, contract.type_name) for contract in text_contracts] == [
        ("atomic", "string")
    ]


def test_unique_list_and_qname_attributes_ignore_element_content_contracts():
    listed = typed(
        compile_schema(
            '<element name="doc"><attribute name="pair"><list><oneOrMore>'
            f"{data('integer')}</oneOrMore></list></attribute>{data('string')}"
            "</element>"
        ),
        io.StringIO('<doc pair="1 2">x</doc>'),
    )
    pair = listed.root.attribute_value("pair")
    assert not isinstance(pair, AlternativeValue)
    assert tuple(pair) == (1, 2)
    pair_site = attribute_occurrence(listed, listed.root, "pair")
    pair_contracts = listed.binding_evidence.schema_contracts_at(pair_site)
    assert [contract.kind for contract in pair_contracts] == ["list"]
    tokens = [
        occurrence
        for occurrence in listed.binding_evidence.occurrences
        if occurrence.kind == "token"
    ]
    assert len(tokens) == 2
    for token in tokens:
        assert token.parent is pair_site
        assert [
            (contract.kind, contract.type_name)
            for contract in listed.binding_evidence.schema_contracts_at(token)
        ] == [("atomic", "integer")]

    qnamed = typed(
        compile_schema(
            '<element name="doc"><attribute name="q">'
            f"{data('QName')}</attribute>{data('string')}</element>"
        ),
        io.StringIO('<doc xmlns:p="urn:a" q="p:a">x</doc>'),
    )
    qname = qnamed.root.attribute_value("q")
    assert not isinstance(qname, AlternativeValue)
    assert qname.to_expanded_name() == "{urn:a}a"
    qname_contracts = qnamed.binding_evidence.schema_contracts_at(
        attribute_occurrence(qnamed, qnamed.root, "q")
    )
    assert [(contract.kind, contract.type_name) for contract in qname_contracts] == [
        ("atomic", "QName")
    ]


def test_empty_attribute_value_stays_lexical_and_unasserted():
    compiled = compile_schema(
        '<element name="doc"><optional><attribute name="flag"><empty/></attribute>'
        f"</optional>{data('string')}</element>"
    )
    document = typed(compiled, io.StringIO('<doc flag="">x</doc>'))

    site = attribute_occurrence(document, document.root, "flag")
    assert document.binding_evidence.schema_contracts_at(site) == ()
    assert document.binding_evidence.scalar_witnesses_at(site) == ()
    assert document.root.attribute_value("flag") is None
    assert document.root.attribute_lexical("flag") == ""


def test_nested_attribute_value_choices_record_every_value_pattern():
    compiled = compile_schema(
        '<element name="doc"><attribute name="a"><choice>'
        f"{data('integer')}<list><oneOrMore>{data('token')}"
        f"</oneOrMore></list></choice></attribute>{data('string')}</element>"
    )
    document = typed(compiled, io.StringIO('<doc a="1 2">x</doc>'))

    site = attribute_occurrence(document, document.root, "a")
    contracts = document.binding_evidence.schema_contracts_at(site)
    assert {(contract.kind, contract.type_name) for contract in contracts} == {
        ("atomic", "integer"),
        ("list", "list"),
    }
    value = document.root.attribute_value("a")
    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 1
    assert document.root.scalar_value() == "x"

    # The wrapper shape stays schema-static: a lexical value that only the
    # list branch accepts still keeps the complete two-contract wrapper.
    one = typed(compiled, io.StringIO('<doc a="1">x</doc>'))
    one_value = one.root.attribute_value("a")
    assert isinstance(one_value, AlternativeValue)
    assert len(one_value.candidates) == 2


def test_trusted_xmlns_attribute_has_no_typed_attribute_site():
    compiled = compile_schema(element("doc"))
    trusted = ET.Element("doc", {"xmlns:p": "urn:values"})

    document = typed(compiled, trusted)

    assert document.is_valid
    assert document.root.attribute_lexical("xmlns:p") == "urn:values"
    assert document.root.attribute_value("xmlns:p") is None


def test_list_attribute_values_use_the_attribute_scalar_site():
    compiled = compile_schema(
        '<element name="doc"><attribute name="pair"><list>'
        f'{data("integer")}<data type="token"/></list></attribute></element>'
    )
    document = typed(compiled, io.StringIO('<doc pair="12 abc"/>'))

    value = document.root.attribute_value("pair")

    assert value == (12, "abc")
    assert type(value) is tuple


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ('<doc a="12"><x/></doc>', 12),
        ('<doc a="12"><y/></doc>', "12"),
    ],
)
def test_late_pruned_attribute_still_uses_the_schema_static_wrapper(xml, expected):
    compiled = compile_schema(
        '<element name="doc"><choice>'
        f'<group><attribute name="a">{data("integer")}</attribute>'
        '<element name="x"><empty/></element></group>'
        f'<group><attribute name="a">{data("string")}</attribute>'
        '<element name="y"><empty/></element></group>'
        "</choice></element>"
    )
    document = typed(compiled, io.StringIO(xml))

    value = document.root.attribute_value("a")

    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 1
    assert value.as_python() == expected
    contracts = document.binding_evidence.schema_contracts_at(
        attribute_occurrence(document, document.root, "a")
    )
    assert {(contract.kind, contract.type_name) for contract in contracts} == {
        ("atomic", "integer"),
        ("atomic", "string"),
    }


def test_empty_atomic_attribute_is_a_real_value_not_absent():
    compiled = compile_schema(
        '<element name="doc"><optional><attribute name="a">'
        f"{data('string')}</attribute></optional><empty/></element>"
    )

    absent = typed(compiled, io.StringIO("<doc/>"))
    assert absent.root.attribute_value("a") is None

    explicit = typed(compiled, io.StringIO('<doc a=""/>'))
    value = explicit.root.attribute_value("a")
    assert value == ""
    assert value is not None
    assert isinstance(value, str)


def test_fixed_and_homogeneous_list_positions_return_ordinary_sequences():
    fixed = compile_schema(
        f'<element name="doc"><list>{data("integer")}<data type="token"/></list></element>'
    )
    fixed_value = typed(fixed, io.StringIO("<doc>12 abc</doc>")).root.scalar_value()
    assert fixed_value == (12, "abc")
    assert type(fixed_value) is tuple

    homogeneous = compile_schema(
        f'<element name="doc"><list><zeroOrMore>{data("integer")}</zeroOrMore></list></element>'
    )
    list_value = typed(homogeneous, io.StringIO("<doc>1 2 3</doc>")).root.scalar_value()
    assert list_value == [1, 2, 3]
    assert type(list_value) is list

    empty = ET.Element("doc")
    empty.text = ""
    empty_value = typed(homogeneous, empty).root.scalar_value()
    assert empty_value == []
    assert type(empty_value) is list


def test_mixed_list_and_atomic_contracts_wrap_the_list_interpretation():
    compiled = compile_schema(
        element("doc", f"<choice><list>{data('integer')}</list>{data('string')}</choice>")
    )
    document = typed(compiled, io.StringIO("<doc>12</doc>"))

    value = document.root.scalar_value()

    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 2
    list_candidate = next(
        candidate for candidate in value.candidates if candidate.contract.kind == "list"
    )
    interpretation = value.as_interpretation(list_candidate.candidate_id)
    assert isinstance(interpretation, ListInterpretation)
    assert interpretation.as_python() == (12,)
    assert value.as_candidate(list_candidate.candidate_id) == (12,)
    assert value.as_type("xsd:string") == "12"
    with pytest.raises(relaxng.AmbiguousValueError):
        value.as_python()


def test_ambiguous_single_list_contract_wraps_its_interpretation():
    compiled = compile_schema(
        element(
            "doc",
            f"<list><choice>{data('integer')}{data('string')}</choice></list>",
        )
    )
    document = typed(compiled, io.StringIO("<doc>12</doc>"))

    value = document.root.scalar_value()

    assert isinstance(value, AlternativeValue)
    (candidate,) = value.candidates
    assert candidate.contract.kind == "list"
    interpretation = value.as_interpretation(candidate.candidate_id)
    assert len(interpretation.token_candidates[0]) == 2
    with pytest.raises(relaxng.AmbiguousValueError):
        value.as_python()


def test_absent_text_is_none_but_explicitly_empty_text_is_a_real_value():
    compiled = compile_schema(element("doc", data("string")))

    absent = typed(compiled, io.StringIO("<doc/>"))
    assert absent.root.text_value() is None
    assert absent.root.scalar_value() is None

    explicit = ET.Element("doc")
    explicit.text = ""
    explicit_document = typed(compiled, explicit)
    assert explicit_document.root.text_value() == ""
    assert explicit_document.root.scalar_value() == ""


def test_ignored_mixed_content_whitespace_has_no_scalar_interpretation():
    compiled = compile_schema(
        '<element name="doc"><mixed><zeroOrMore>'
        '<element name="item"><empty/></element>'
        "</zeroOrMore></mixed></element>"
    )
    document = typed(compiled, io.StringIO("<doc> <item/> </doc>"))

    assert document.is_valid
    assert document.root.text_value() == " "
    assert document.root.scalar_value() is None


def test_invalid_typed_documents_have_no_evidence_and_reject_values():
    document = typed(scalar_schema(), io.StringIO("<doc>not-an-integer</doc>"))

    assert document.typed is True
    assert document.is_valid is False
    assert document.binding_evidence is None
    assert document.interpretations_current is True

    for call in (
        document.root.scalar_value,
        lambda: document.root.attribute_value("missing"),
    ):
        with pytest.raises(ValidationError) as excinfo:
            call()
        assert excinfo.value.report is document.report

    document.root.set_text("still-not-an-integer")
    with pytest.raises(StaleInterpretationError):
        document.root.scalar_value()


def test_dirty_typed_revision_rejects_reads_and_keeps_old_snapshots():
    document = typed(late_branch_schema(), io.StringIO("<doc><x>12</x><b/></doc>"))
    node = document.root.children[0]
    value_before = node.scalar_value()
    assert isinstance(value_before, AlternativeValue)
    candidate_id = value_before.candidates[0].candidate_id
    evidence = document.binding_evidence
    report = document.report

    node.set_text("99")

    assert document.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        node.scalar_value()
    with pytest.raises(StaleInterpretationError):
        node.attribute_value("missing")
    assert document.binding_evidence is evidence
    assert value_before.lexical == "12"
    assert value_before.candidates[0].candidate_id == candidate_id
    assert document.report is report

    fresh = document.revalidate()

    assert fresh is not document
    assert fresh.typed is True
    assert fresh.interpretations_current is True
    assert fresh.binding_evidence is not evidence
    fresh_value = fresh.root.children[0].scalar_value()
    assert isinstance(fresh_value, AlternativeValue)
    assert fresh_value.lexical == "99"
    assert fresh_value.candidates[0].candidate_id == candidate_id
    assert document.interpretations_current is False


def test_typed_transform_carries_stale_evidence_without_refreshing_it():
    document = typed(scalar_schema(), io.StringIO("<doc>12</doc>"))
    evidence = document.binding_evidence
    assert isinstance(evidence, EvidenceGraph)

    stale = document.transform(lambda root: root)

    assert stale is not document
    assert stale.typed is True
    assert stale.interpretations_current is False
    assert stale.binding_evidence is evidence

    fresh = stale.revalidate()

    assert fresh.interpretations_current is True
    assert fresh.typed is True
    assert fresh.binding_evidence is not evidence

    with pytest.raises(AttributeError):
        document.binding_evidence = None  # type: ignore[misc]


def test_second_document_cannot_replace_or_hide_revision_evidence():
    compiled = scalar_schema()
    document = typed(compiled, io.StringIO("<doc>12</doc>"))
    evidence = document.binding_evidence
    report = document.report

    invalid = compiled.parse(io.StringIO("<doc>words</doc>"), typed=True)
    with pytest.raises(ValueError, match="different typed"):
        RelaxNGDocument(
            schema=document.schema,
            root=document.root,
            source=document.source,
            report=invalid.report,
            typed=True,
        )

    other = typed(compiled, io.StringIO("<doc>12</doc>"))
    with pytest.raises(ValueError, match="different typed"):
        RelaxNGDocument(
            schema=document.schema,
            root=document.root,
            source=document.source,
            report=other.report,
            typed=True,
            binding_evidence=other.binding_evidence,
        )

    with pytest.raises(ValueError, match="typed evidence"):
        RelaxNGDocument(
            schema=document.schema,
            root=document.root,
            source=document.source,
            report=report,
            typed=False,
        )

    assert document.binding_evidence is evidence
    assert document.report is report
    assert document.root.scalar_value() == 12
    assert document.interpretations_current is True


def test_public_constructor_rejects_foreign_binding_evidence():
    compiled = scalar_schema()
    plain = validation_only(compiled, "<doc>12</doc>")
    donor = typed(compiled, io.StringIO("<doc>12</doc>"))
    graph = donor.binding_evidence
    assert graph is not None
    with pytest.raises(PyXSDError, match="typed=True"):
        plain.root.scalar_value()

    owned = build_binding_evidence(
        plain.root, compiled.grammar.start, datatype_context=DatatypeContext(None, {}), limits=None
    )
    for evidence in (
        graph,
        replace(owned, occurrences=tuple(o for o in owned.occurrences if o.kind != "document")),
        replace(owned, occurrences=(*owned.occurrences, EvidenceOccurrence(donor.root, "text"))),
        replace(owned, retained_nodes=(*owned.retained_nodes, graph.retained_nodes[0])),
        replace(owned, accepting_roots=graph.accepting_roots),
        replace(owned, retained_edges=(*owned.retained_edges, *graph.retained_edges)),
        replace(owned, _schema_patterns=graph._schema_patterns),
        replace(
            owned,
            occurrences=(
                *owned.occurrences,
                EvidenceOccurrence(
                    plain.root, "token", parent=EvidenceOccurrence(donor.root, "text")
                ),
            ),
        ),
    ):
        for current in (True, False):
            with pytest.raises(ValueError, match=r"evidence.*root.*revision"):
                RelaxNGDocument(
                    compiled,
                    plain.root,
                    plain.source,
                    plain.report,
                    typed=True,
                    binding_evidence=evidence,
                    interpretations_current=current,
                )
    with pytest.raises(PyXSDError, match="typed=True"):
        plain.root.scalar_value()
    with pytest.raises(ValueError, match="different typed"):
        RelaxNGDocument(
            compiled,
            donor.root,
            donor.source,
            donor.report,
            typed=True,
            binding_evidence=owned,
        )
    with pytest.raises(ValueError, match="different typed"):
        plain_typed = typed(compiled, io.StringIO("<doc>13</doc>"))
        donor.transform(lambda root: plain_typed.root)
    with pytest.raises(ValueError, match="typed evidence"):
        plain.transform(lambda root: donor.root)
    assert donor.root.scalar_value() == 12


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("existing", [True, False])
def test_public_constructor_false_current_flag_gates_node_reads(valid, existing):
    compiled = scalar_schema()
    parsed = compiled.parse(
        io.StringIO("<doc>12</doc>" if valid else "<doc>bad</doc>"), typed=existing
    )
    evidence = parsed.binding_evidence
    if valid and not existing:
        evidence = build_binding_evidence(
            parsed.root,
            compiled.grammar.start,
            datatype_context=DatatypeContext(None, {}),
            limits=None,
        )
    stale = RelaxNGDocument(
        compiled,
        parsed.root,
        parsed.source,
        parsed.report,
        typed=True,
        binding_evidence=evidence,
        interpretations_current=False,
    )
    assert stale.interpretations_current is False
    for read in (stale.root.scalar_value, lambda: stale.root.attribute_value("missing")):
        with pytest.raises(StaleInterpretationError):
            read()
    assert stale.revalidate().interpretations_current is True
    transformed = stale.transform(lambda root: clone_relaxng(root))
    for read in (
        transformed.root.scalar_value,
        lambda: transformed.root.attribute_value("missing"),
    ):
        with pytest.raises(StaleInterpretationError):
            read()
    with pytest.raises(TypeError, match=r"interpretations_current.*bool"):
        RelaxNGDocument(
            compiled,
            stale.root,
            stale.source,
            stale.report,
            typed=True,
            binding_evidence=evidence,
            interpretations_current=0,
        )


def test_public_constructor_accepts_evidence_owned_by_hand_built_root():
    compiled = scalar_schema()
    root = RelaxNGElement.from_etree(ET.fromstring("<doc>12</doc>"))
    evidence = build_binding_evidence(
        root, compiled.grammar.start, datatype_context=DatatypeContext(None, {}), limits=None
    )
    report = validation_only(compiled, "<doc>12</doc>").report
    document = RelaxNGDocument(compiled, root, None, report, typed=True, binding_evidence=evidence)
    shared = RelaxNGDocument(compiled, root, None, report, typed=True, binding_evidence=evidence)
    assert document.binding_evidence is shared.binding_evidence is evidence
    assert document.interpretations_current is shared.interpretations_current is True
    assert root.scalar_value() == 12


def test_same_root_transform_stales_the_shared_revision_and_original():
    document = typed(text_schema(), io.StringIO("<doc>hello</doc>"))
    evidence = document.binding_evidence
    report = document.report

    stale = document.transform(lambda root: root)

    assert isinstance(stale, RelaxNGDocument)
    assert stale.typed is True
    assert stale.interpretations_current is False
    assert document.interpretations_current is False
    for target in (stale.root, document.root):
        with pytest.raises(StaleInterpretationError):
            target.scalar_value()
        with pytest.raises(StaleInterpretationError):
            target.attribute_value("missing")
    assert document.binding_evidence is evidence
    assert document.report is report

    fresh = stale.revalidate()

    assert fresh.interpretations_current is True
    assert fresh.typed is True
    assert fresh.binding_evidence is not evidence
    assert str(fresh.root.scalar_value()) == "hello"
    assert document.interpretations_current is False


def test_clone_transform_attaches_a_stale_marker_to_the_clone_revision():
    document = typed(text_schema(), io.StringIO("<doc>hello</doc>"))
    evidence = document.binding_evidence

    stale = document.transform(lambda root: clone_relaxng(root))

    assert isinstance(stale, RelaxNGDocument)
    assert stale.root is not document.root
    assert stale.root.revision is not document.root.revision
    assert stale.typed is True
    assert stale.interpretations_current is False
    assert stale.binding_evidence is evidence
    with pytest.raises(StaleInterpretationError):
        stale.root.scalar_value()
    with pytest.raises(StaleInterpretationError):
        stale.root.attribute_value("missing")

    assert document.interpretations_current is True
    assert str(document.root.scalar_value()) == "hello"

    fresh = stale.revalidate()

    assert fresh.interpretations_current is True
    assert str(fresh.root.scalar_value()) == "hello"


def test_a_revision_rejects_replacing_its_typed_snapshot():
    document = typed(scalar_schema(), io.StringIO("<doc>12</doc>"))
    revision = document.root.revision
    snapshot = revision._typed_snapshot
    assert snapshot is not None

    revision._attach_typed_snapshot(snapshot)
    with pytest.raises(ValueError, match="already owns"):
        revision._attach_typed_snapshot(object())  # type: ignore[arg-type]
    assert revision._typed_snapshot is snapshot


def test_validation_only_documents_are_evidence_free_and_reject_typed_access():
    document = validation_only(scalar_schema(), "<doc>12</doc>")

    assert document.typed is False
    assert document.binding_evidence is None
    for call in (
        document.root.scalar_value,
        lambda: document.root.attribute_value("missing"),
    ):
        with pytest.raises(PyXSDError, match="typed=True") as excinfo:
            call()
        assert type(excinfo.value) is PyXSDError


def test_capture_setting_does_not_change_typed_values_or_ids():
    compiled = compile_schema(element("doc", f"<choice>{data('integer')}{data('string')}</choice>"))

    without_capture = typed(compiled, io.StringIO("<doc>12</doc>"), capture="none")
    with_capture = typed(compiled, io.StringIO("<doc>12</doc>"), capture="patterns")

    first = without_capture.root.scalar_value()
    second = with_capture.root.scalar_value()
    assert isinstance(first, AlternativeValue)
    assert isinstance(second, AlternativeValue)
    assert without_capture._capture == "none"
    assert with_capture._capture == "patterns"
    assert tuple(candidate.candidate_id for candidate in first.candidates) == tuple(
        candidate.candidate_id for candidate in second.candidates
    )
    assert [candidate.decoded for candidate in first.candidates] == [
        candidate.decoded for candidate in second.candidates
    ]
    assert first.grammar_fingerprint == second.grammar_fingerprint
    assert first.context == second.context

    with pytest.raises(ValueError, match="typed=True"):
        compiled.parse(io.StringIO("<doc>12</doc>"), typed=False, capture="patterns")


def test_mandatory_evidence_is_built_only_for_valid_typed_parses():
    compiled = scalar_schema()

    with pytest.raises(RelaxNGLimitError) as excinfo:
        compiled.parse(io.StringIO("<doc>12</doc>"), limits=RelaxNGLimits(max_evidence_states=0))
    assert excinfo.value.stage == "evidence"
    assert excinfo.value.budget == "max_evidence_states"

    validation = compiled.parse(
        io.StringIO("<doc>12</doc>"),
        typed=False,
        limits=RelaxNGLimits(max_evidence_states=0),
    )
    assert validation.is_valid
    assert validation.binding_evidence is None

    invalid = compiled.parse(
        io.StringIO("<doc>words</doc>"),
        limits=RelaxNGLimits(max_evidence_states=0),
    )
    assert invalid.is_valid is False
    assert invalid.binding_evidence is None

    captured = compiled.parse(
        io.StringIO("<doc>12</doc>"),
        capture="patterns",
        limits=RelaxNGLimits(max_capture_entries=0),
    )
    assert captured.root.scalar_value() == 12


def test_the_package_exports_the_scalar_value_contracts():
    assert relaxng.AlternativeValue is AlternativeValue
    assert relaxng.ListInterpretation is ListInterpretation
    assert {"AlternativeValue", "ListInterpretation"} <= set(relaxng.__all__)
