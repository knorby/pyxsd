"""Deterministic RELAX NG expectation summaries and first-mismatch findings."""

import json
import os
import subprocess
import sys
from xml.etree import ElementTree as ET

import pytest

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.relaxng import validator as v
from pyxsd.relaxng.datatypes import (
    BuiltinDatatypeFactory,
    Datatype,
    DatatypeContext,
    DatatypeParameter,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.diagnostics import (
    INVALID_SCALAR,
    INVALID_STRUCTURE,
    MISSING_ATTRIBUTE,
    MISSING_CONTENT,
    UNEXPECTED_ATTRIBUTE,
    UNEXPECTED_CONTENT,
    UNEXPECTED_ELEMENT,
    ExpectationSummary,
    expectations,
    report_first_mismatch,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.names import (
    AnyName,
    ExceptName,
    Name,
    NameChoice,
    NameClass,
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
    Pattern,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.validation import ValidationReport

CX = DatatypeContext(None, {})
STRING = BuiltinDatatypeFactory().create("string", (), CX)
INTEGER = XsdDatatypeFactory().create("integer", (), CX)


def element(name, content=None):
    name_class = name if isinstance(name, Name) else Name(None, name)
    return Element(name_class, Empty() if content is None else content)


def attribute(name, content=None):
    name_class = name if isinstance(name, Name) else Name(None, name)
    return Attribute(name_class, Text() if content is None else content)


def tree(xml, source="instance.xml"):
    return RelaxNGElement.from_etree(
        ET.fromstring(xml), source=IssueLocation(source, ()) if source else None
    )


def report_for(xml, pattern, *, source="instance.xml", limits=None):
    outcome = v.validate_element(tree(xml, source), pattern, limits=limits)
    assert not outcome.valid
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    assert len(report) == 1
    return outcome, report.issues[0]


# ---------------------------------------------------------------------------
# Expectation projection


def test_expectations_of_not_allowed_is_empty_and_empty_is_its_own_label():
    assert expectations(NotAllowed()) == ExpectationSummary()
    assert expectations(Empty()).content == ("empty",)
    assert expectations(Empty()).elements == ()


def test_expectations_sorts_choice_and_interleave_names():
    first = Choice(
        Interleave(element("c"), element("a")),
        Group(element("b"), element("z")),
    )
    second = Choice(
        Group(element("b"), element("z")),
        Interleave(element("a"), element("c")),
    )
    assert expectations(first).elements == ("a", "b", "c")
    assert expectations(second).elements == ("a", "b", "c")
    assert expectations(first) == expectations(second)


def test_expectations_use_the_first_group_operand_until_it_is_nullable():
    assert expectations(Group(element("a"), element("b"))).elements == ("a",)
    assert expectations(Group(Choice(Empty(), element("a")), element("b"))).elements == (
        "a",
        "b",
    )
    assert expectations(Group(Empty(), element("b"))).elements == ("b",)


def test_expectations_project_both_interleave_operands_independent_of_order():
    left = Interleave(element("c"), Group(element("b"), element("a")))
    right = Interleave(Group(element("b"), element("a")), element("c"))
    assert expectations(left).elements == ("b", "c")
    assert expectations(right).elements == ("b", "c")


def test_expectations_follow_recursive_references_without_looping():
    ref = Ref()
    ref.bind(Choice(Empty(), element("leaf", ref)))
    summary = expectations(ref)
    assert summary.elements == ("leaf",)
    assert summary.content == ("empty",)


def test_expectations_follow_recursive_reference_cycles_at_the_current_position():
    ref = Ref()
    ref.bind(Group(Empty(), ref))
    summary = expectations(ref)
    assert summary.elements == ()
    assert summary.content == ("empty",)


def test_expectations_include_repetition_and_ignore_elements_in_attributes():
    summary = expectations(OneOrMore(Attribute(Name(None, "a"), Data(INTEGER))))
    assert summary.attributes == ("a",)
    assert summary.datatypes == ("xsd:integer",)
    assert expectations(OneOrMore(element("a"))).elements == ("a",)


def test_expectation_summary_describe_is_stable_and_covers_empty_fields():
    assert ExpectationSummary().describe() == "no expectation"
    assert ExpectationSummary(elements=("a",)).describe() == "elements a"
    assert (
        ExpectationSummary(
            elements=("a",),
            attributes=("b",),
            content=("text",),
            datatypes=("string",),
        ).describe()
        == "elements a; attributes b; content text; datatypes string"
    )
    assert (
        ExpectationSummary(
            elements=("a",),
            attributes=("b",),
            content=("text",),
        ).describe()
        == "elements a; attributes b; content text"
    )


def test_expectations_walk_deep_nullable_groups_iteratively():
    pattern = element("deep")
    for _ in range(2000):
        pattern = Group(Empty(), pattern)
    summary = expectations(pattern)
    assert summary.elements == ("deep",)
    assert summary.content == ("empty",)


def test_expectations_sort_attribute_names_and_ignore_attribute_order():
    first = v._After(
        Group(
            Attribute(NameChoice((Name(None, "b"), Name(None, "a"))), Text()),
            Attribute(Name(None, "c"), Text()),
        ),
        Empty(),
    )
    second = v._After(
        Group(
            Attribute(Name(None, "c"), Text()),
            Attribute(NameChoice((Name(None, "a"), Name(None, "b"))), Text()),
        ),
        Empty(),
    )
    assert expectations(first).attributes == ("a", "b", "c")
    assert expectations(second).attributes == ("a", "b", "c")


def test_expectations_include_namespaced_names_in_sorted_expanded_form():
    pattern = Choice(
        Element(Name("urn:z", "z"), Empty()),
        Choice(
            Element(Name("urn:a", "a"), Empty()),
            Element(Name(None, "plain"), Empty()),
        ),
    )
    assert expectations(pattern).elements == ("plain", "{urn:a}a", "{urn:z}z")
    assert v.expanded_name("{urn:a}a") == ("urn:a", "a")
    assert v.expanded_name("plain") == (None, "plain")


def test_expectations_summarize_scalar_content_and_datatypes():
    pattern = Choice(
        Text(),
        Choice(
            Data(INTEGER),
            Choice(
                Value(STRING, "fixed"),
                Choice(
                    List(Data(INTEGER)),
                    Choice(
                        DataExcept(INTEGER, Value(INTEGER, "3")),
                        Empty(),
                    ),
                ),
            ),
        ),
    )
    summary = expectations(pattern)
    assert summary.content == ("data", "empty", "list", "text", "value 'fixed'")
    assert summary.datatypes == ("string", "xsd:integer")


def test_expectations_include_attribute_value_datatypes():
    pattern = v._After(Attribute(Name(None, "n"), Data(INTEGER)), Empty())
    summary = expectations(pattern)
    assert summary.attributes == ("n",)
    assert summary.datatypes == ("xsd:integer",)
    assert summary.content == ()


def test_expectations_describe_wildcard_and_except_name_classes():
    assert expectations(Element(AnyName(), Empty())).elements == ("*",)
    assert expectations(Element(NsName("urn:n"), Empty())).elements == ("{urn:n}*",)
    assert expectations(Element(NsName(None), Empty())).elements == ("{}*",)
    assert expectations(
        Element(ExceptName(NsName("urn:n"), Name("urn:n", "no")), Empty())
    ).elements == ("{urn:n}* except {urn:n}no",)
    assert expectations(Element(ExceptName(NsName("urn:n"), NameChoice(())), Empty())).elements == (
        "{urn:n}*",
    )


def test_expectations_reject_unknown_pattern_and_name_class_nodes():
    class UnknownPattern(Pattern):
        pass

    class UnknownNameClass(NameClass):
        pass

    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        expectations(UnknownPattern())
    with pytest.raises(TypeError, match="unknown RELAX NG name class"):
        expectations(Element(UnknownNameClass(), Empty()))
    with pytest.raises(TypeError, match="RELAX NG pattern"):
        expectations(object())


def test_datatype_summaries_cover_library_names_context_and_parameters():
    class LibraryDatatype(Datatype):
        library_uri = "urn:custom"
        type_name = "widget"
        parameters = ()

        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

    class AnonymousDatatype(Datatype):
        parameters = ()

        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

    class UnknownParametersDatatype(LibraryDatatype):
        parameters = "not-a-sequence"

    class BadParameter:
        pass

    class BadParameterDatatype(LibraryDatatype):
        parameters = (BadParameter(),)

    from pyxsd.relaxng import diagnostics as d

    assert d._datatype_label(STRING) == "string"
    assert d._datatype_label(INTEGER) == "xsd:integer"
    assert d._datatype_label(LibraryDatatype()) == "{urn:custom}widget"
    assert d._datatype_label(AnonymousDatatype()) == "AnonymousDatatype"
    assert d._datatype_label(UnknownParametersDatatype()) == "{urn:custom}widget"
    assert d._datatype_label(BadParameterDatatype()) == "{urn:custom}widget"
    parameterized = XsdDatatypeFactory().create(
        "integer", (DatatypeParameter("minInclusive", "1"),), CX
    )
    assert d._datatype_label(parameterized) == "xsd:integer(minInclusive='1')"


def test_expectations_cover_all_datatype_projection_node_kinds():
    from pyxsd.relaxng import diagnostics as d

    class UnknownPattern(Pattern):
        pass

    assert d._datatype_descriptions(Data(INTEGER)) == {"xsd:integer"}
    assert d._datatype_descriptions(DataExcept(INTEGER, Text())) == {"xsd:integer"}
    assert d._datatype_descriptions(Value(STRING, "x")) == {"string"}
    assert d._datatype_descriptions(List(Data(INTEGER))) == {"xsd:integer"}
    assert d._datatype_descriptions(OneOrMore(Data(INTEGER))) == {"xsd:integer"}
    assert d._datatype_descriptions(Group(Data(INTEGER), Text())) == {"xsd:integer"}
    assert d._datatype_descriptions(Choice(Data(INTEGER), Text())) == {"xsd:integer"}
    assert d._datatype_descriptions(Interleave(Data(INTEGER), Text())) == {"xsd:integer"}
    ref = Ref()
    ref.bind(Data(INTEGER))
    assert d._datatype_descriptions(ref) == {"xsd:integer"}
    cyclic = Ref()
    cyclic.bind(Choice(Data(INTEGER), cyclic))
    assert d._datatype_descriptions(cyclic) == {"xsd:integer"}
    assert d._datatype_descriptions(v._After(Data(INTEGER), Empty())) == {"xsd:integer"}
    assert d._datatype_descriptions(element("a")) == set()
    assert d._datatype_descriptions(attribute("a")) == set()
    assert d._datatype_descriptions(Text()) == set()
    assert d._datatype_descriptions(Empty()) == set()
    assert d._datatype_descriptions(NotAllowed()) == set()
    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        d._datatype_descriptions(UnknownPattern())


# ---------------------------------------------------------------------------
# First-mismatch findings


def test_unexpected_element_reports_earliest_occurrence_and_expectation():
    pattern = element("r", Group(element("a"), element("b")))
    root = tree("<r><a/><wrong/><b/></r>")
    outcome = v.validate_element(root, pattern)
    assert not outcome.valid
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    issue = report.issues[0]
    assert issue.code == UNEXPECTED_ELEMENT == "unexpected-element"
    assert issue.phase == "instance"
    assert issue.element == "wrong"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1), PathStep("wrong", 1)))
    assert issue.location_precision == "exact"
    assert issue.details == {
        "event": "start_tag_open",
        "expected-elements": ["b"],
        "expected-attributes": [],
        "expected-content": [],
        "expected-datatypes": [],
        "actual-name": "wrong",
    }
    assert "element 'wrong'" in issue.message
    assert "b" in issue.message
    assert outcome.mismatch.element is root.children[1]
    assert expectations(outcome.mismatch.preceding).elements == ("b",)
    assert issue.location is not None


def test_unexpected_root_element_uses_the_occurrence_and_start_pattern():
    pattern = element("r")
    outcome, issue = report_for("<wrong/>", pattern)
    assert issue.code == UNEXPECTED_ELEMENT
    assert issue.element == "wrong"
    assert issue.location == IssueLocation("instance.xml", (PathStep("wrong", 1),))
    assert issue.details["expected-elements"] == ["r"]
    assert outcome.mismatch.element.source_location == issue.location


def test_unexpected_attribute_reports_the_attribute_occurrence():
    pattern = element("r")
    _outcome, issue = report_for('<r a="x"/>', pattern)
    assert issue.code == UNEXPECTED_ATTRIBUTE == "unexpected-attribute"
    assert issue.element == "r"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),), attribute="a")
    assert issue.location_precision == "exact"
    assert issue.details["expected-attributes"] == []
    assert issue.details["actual-name"] == "a"
    assert issue.details["actual-lexical"] == "x"
    assert "attribute 'a'" in issue.message


def test_invalid_attribute_scalar_is_not_an_unexpected_attribute():
    pattern = element("r", attribute("a", Data(INTEGER)))
    _outcome, issue = report_for('<r a="x"/>', pattern)
    assert issue.code == INVALID_SCALAR == "invalid-scalar"
    assert issue.element == "r"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),), attribute="a")
    assert issue.details["expected-attributes"] == ["a"]
    assert issue.details["expected-datatypes"] == ["xsd:integer"]
    assert "not a valid scalar value" in issue.message


def test_wildcard_attribute_is_expected_and_scalar_failure_is_classified():
    pattern = element("r", Attribute(AnyName(), Data(INTEGER)))
    _outcome, issue = report_for('<r a="x"/>', pattern)
    assert issue.code == INVALID_SCALAR
    assert issue.details["expected-attributes"] == ["*"]
    assert issue.location is not None
    assert issue.location.attribute == "a"


def test_attribute_scalar_message_does_not_borrow_content_expectations():
    pattern = element("r", Choice(Attribute(Name(None, "a"), Data(INTEGER)), Text()))
    _outcome, issue = report_for('<r a="bad"/>', pattern)
    assert issue.code == INVALID_SCALAR
    assert "expected datatype xsd:integer" in issue.message
    assert "expected text" not in issue.message

    pattern = element("r", Attribute(Name(None, "a"), Empty()))
    _outcome, issue = report_for('<r a="x"/>', pattern)
    assert issue.code == INVALID_SCALAR
    assert "expected a valid scalar value" in issue.message


def test_namespaced_expected_attribute_uses_expanded_names():
    root_name = Name("urn:r", "r")
    attr_name = Name("urn:a", "a")
    pattern = Element(root_name, Attribute(attr_name, Data(INTEGER)))
    _outcome, issue = report_for('<r:r xmlns:r="urn:r" xmlns:a="urn:a" a:a="x"/>', pattern)
    assert issue.element == "{urn:r}r"
    assert issue.location == IssueLocation(
        "instance.xml", (PathStep("{urn:r}r", 1),), attribute="{urn:a}a"
    )
    assert issue.details["expected-attributes"] == ["{urn:a}a"]


def test_attribute_classification_traverses_groups_repetition_and_references():
    group_pattern = element("r", Group(attribute("b"), attribute("a", Data(INTEGER))))
    _outcome, issue = report_for('<r b="ok" a="bad"/>', group_pattern)
    assert issue.code == INVALID_SCALAR
    assert issue.details["expected-attributes"] == ["a", "b"]

    repeated = element("r", OneOrMore(attribute("a", Data(INTEGER))))
    _outcome, issue = report_for('<r a="bad"/>', repeated)
    assert issue.code == INVALID_SCALAR
    assert issue.details["expected-attributes"] == ["a"]

    ref = Ref()
    ref.bind(Attribute(Name(None, "a"), Data(INTEGER)))
    _outcome, issue = report_for('<r a="bad"/>', element("r", ref))
    assert issue.code == INVALID_SCALAR

    _outcome, issue = report_for('<r a="x"/>', element("r", Group(attribute("b"), element("n"))))
    assert issue.code == UNEXPECTED_ATTRIBUTE
    assert issue.details["expected-attributes"] == ["b"]

    from pyxsd.relaxng import diagnostics as d

    cyclic = Ref()
    cyclic.bind(Group(Empty(), cyclic))
    assert d._expects_attribute_name(cyclic, None, "a") is False


def test_attribute_classification_missing_name_and_unknown_pattern_are_handled():
    from pyxsd.relaxng import diagnostics as d

    class UnknownPattern(Pattern):
        pass

    root = tree("<r/>")
    outcome = v.ValidationOutcome(
        False,
        NotAllowed(),
        v.ValidationMismatch("attribute", root, NotAllowed(), lexical="x"),
    )
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    assert report.issues[0].code == UNEXPECTED_ATTRIBUTE
    assert "actual-name" not in report.issues[0].details
    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        d._expects_attribute_name(UnknownPattern(), None, "a")
    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        d._attribute_projection(UnknownPattern())
    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        d._content_projection(UnknownPattern())


def test_missing_attribute_reports_the_container_location_and_all_names():
    pattern = element("r", Group(attribute("b"), attribute("a")))
    outcome, issue = report_for("<r/>", pattern)
    assert outcome.mismatch.event == "start_tag_close"
    assert issue.code == MISSING_ATTRIBUTE == "missing-attribute"
    assert issue.element == "r"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),))
    assert issue.location_precision == "container"
    assert issue.details["expected-attributes"] == ["a", "b"]
    assert "a, b" in issue.message


def test_invalid_text_scalar_keeps_the_content_index_and_datatype():
    pattern = element("r", Data(INTEGER))
    _outcome, issue = report_for("<r>x</r>", pattern)
    assert issue.code == INVALID_SCALAR
    assert issue.element == "r"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),))
    assert issue.location_precision == "container"
    assert issue.details["content-index"] == 0
    assert issue.details["actual-lexical"] == "x"
    assert issue.details["expected-datatypes"] == ["xsd:integer"]
    assert "character data 'x'" in issue.message


def test_invalid_text_value_reports_the_expected_lexical_value():
    pattern = element("r", Value(STRING, "yes"))
    _outcome, issue = report_for("<r>no</r>", pattern)
    assert issue.code == INVALID_SCALAR
    assert issue.details["expected-content"] == ["value 'yes'"]
    assert issue.details["expected-datatypes"] == ["string"]


def test_unexpected_text_content_reports_the_expected_model():
    pattern = element("r", Empty())
    _outcome, issue = report_for("<r>x</r>", pattern)
    assert issue.code == UNEXPECTED_CONTENT == "unexpected-content"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),))
    assert issue.location_precision == "container"
    assert issue.details["expected-content"] == ["empty"]
    assert "character data 'x'" in issue.message


def test_unexpected_text_when_elements_are_expected_is_not_a_scalar_failure():
    pattern = element("r", element("a"))
    _outcome, issue = report_for("<r>x</r>", pattern)
    assert issue.code == UNEXPECTED_CONTENT
    assert issue.details["expected-elements"] == ["a"]
    assert issue.details["expected-datatypes"] == []


def test_content_index_identifies_text_after_a_consumed_child():
    pattern = element("r", Group(element("a"), Empty()))
    outcome, issue = report_for("<r><a/>bad</r>", pattern)
    assert outcome.mismatch.event == "text"
    assert issue.code == UNEXPECTED_CONTENT
    assert issue.details["content-index"] == 1
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),))


def test_missing_element_content_is_reported_at_end_tag():
    pattern = element("r", element("a"))
    outcome, issue = report_for("<r/>", pattern)
    assert outcome.mismatch.event == "end_tag"
    assert issue.code == MISSING_CONTENT == "missing-content"
    assert issue.element == "r"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),))
    assert issue.location_precision == "container"
    assert issue.details["expected-elements"] == ["a"]
    assert "required content is missing" in issue.message


def test_missing_scalar_content_is_reported_at_end_tag():
    pattern = element("r", Value(STRING, "x"))
    outcome, issue = report_for("<r/>", pattern)
    assert outcome.mismatch.event == "end_tag"
    assert issue.code == MISSING_CONTENT
    assert issue.details["expected-content"] == ["value 'x'"]
    assert issue.details["expected-datatypes"] == ["string"]


def test_missing_top_level_content_is_reported_at_document_end():
    pattern = Group(element("r"), element("later"))
    outcome, issue = report_for("<r/>", pattern)
    assert outcome.mismatch.event == "document_end"
    assert issue.code == MISSING_CONTENT
    assert issue.element == "r"
    assert issue.location == IssueLocation("instance.xml", (PathStep("r", 1),))
    assert issue.details["expected-elements"] == ["later"]


def test_invalid_document_structure_for_non_element_and_empty_name():
    comment = RelaxNGElement.from_etree(ET.Comment("not an element"))
    outcome = v.validate_element(comment, element("r"))
    assert not outcome.valid
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    issue = report.issues[0]
    assert issue.code == INVALID_STRUCTURE == "invalid-structure"
    assert issue.element is None
    assert issue.location is None
    assert issue.location_precision == "unknown"
    assert issue.details["expected-elements"] == ["r"]
    assert "not an element" in issue.message

    sourced = RelaxNGElement.from_etree(
        ET.Comment("not an element"), source=IssueLocation("instance.xml", ())
    )
    outcome = v.validate_element(sourced, element("r"))
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    assert report.issues[0].location is None
    assert json.loads(report.to_json())["issues"][0]["location"] is None

    empty = RelaxNGElement.from_etree(ET.Element(""))
    outcome = v.validate_element(empty, element("r"))
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    issue = report.issues[0]
    assert issue.code == INVALID_STRUCTURE
    assert "name is empty" in issue.message


def test_missing_source_leaves_location_unknown():
    root = RelaxNGElement.from_etree(ET.fromstring("<r><wrong/></r>"))
    outcome = v.validate_element(root, element("r"))
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    issue = report.issues[0]
    assert issue.code == UNEXPECTED_ELEMENT
    assert issue.location is None
    assert issue.location_precision == "unknown"


def test_valid_outcome_adds_no_issue_and_phase_is_forced_to_instance():
    outcome = v.validate_element(tree("<r/>"), element("r"))
    report = ValidationReport()
    report.phase = "schema"
    report_first_mismatch(outcome, report)
    assert report.issues == []
    assert report.error_count == 0

    invalid = v.validate_element(tree("<wrong/>"), element("r"))
    report = ValidationReport()
    report.phase = "schema"
    report_first_mismatch(invalid, report)
    assert report.issues[0].phase == "instance"


def test_invalid_outcome_without_mismatch_evidence_is_rejected():
    outcome = v.ValidationOutcome(False, NotAllowed(), None)
    with pytest.raises(ValueError, match="mismatch"):
        report_first_mismatch(outcome, ValidationReport())


def test_unknown_mismatch_event_and_missing_element_are_rejected_or_handled():
    root = tree("<r/>")
    outcome = v.ValidationOutcome(
        False, NotAllowed(), v.ValidationMismatch("bogus", root, NotAllowed())
    )
    with pytest.raises(ValueError, match="unknown RELAX NG mismatch event"):
        report_first_mismatch(outcome, ValidationReport())

    outcome = v.ValidationOutcome(
        False, NotAllowed(), v.ValidationMismatch("structure", None, element("r"))
    )
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    assert report.issues[0].code == INVALID_STRUCTURE
    assert report.issues[0].location is None


def test_generic_structure_message_for_a_named_element():
    root = tree("<r/>")
    outcome = v.ValidationOutcome(
        False, NotAllowed(), v.ValidationMismatch("structure", root, element("r"))
    )
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    issue = report.issues[0]
    assert issue.code == INVALID_STRUCTURE
    assert issue.element == "r"
    assert "near element 'r'" in issue.message


def test_expectation_fallbacks_for_empty_residuals_are_deterministic():
    _outcome, issue = report_for("<r/>", NotAllowed())
    assert issue.code == UNEXPECTED_ELEMENT
    assert "no element is expected here" in issue.message

    _outcome, issue = report_for("<r><a/></r>", element("r", Text()))
    assert issue.code == UNEXPECTED_ELEMENT
    assert "expected text" in issue.message

    from pyxsd.relaxng import diagnostics as d

    assert d._content_expectation(ExpectationSummary()) == "no content is expected here"

    _outcome, issue = report_for("<r>x</r>", element("r", List(Empty())))
    assert issue.code == INVALID_SCALAR
    assert "expected list" in issue.message

    _outcome, issue = report_for("<r/>", element("r", Choice(element("a"), Data(INTEGER))))
    assert issue.code == MISSING_CONTENT
    assert "expected element a or content data (xsd:integer)" in issue.message

    assert d._scalar_expectation(ExpectationSummary()) == "expected a valid scalar value"


def test_limit_exhaustion_is_raised_separately_and_never_a_grammar_mismatch():
    report = ValidationReport()
    with pytest.raises(RelaxNGLimitError) as raised:
        v.validate_element(
            tree("<r/>"),
            element("r", attribute("a")),
            limits=RelaxNGLimits(max_derivative_states=0),
        )
    assert raised.value.stage == "validate"
    assert raised.value.budget == "max_derivative_states"
    assert report.issues == []

    with pytest.raises(RelaxNGLimitError) as raised:
        v.validate_element(
            tree("<r/>"),
            element("r", attribute("a")),
            limits=RelaxNGLimits(max_memo_entries=0),
        )
    assert raised.value.budget == "max_memo_entries"


def test_reports_serialize_with_existing_diagnostic_encoding():
    root = tree("<r><a/><wrong/></r>")
    outcome = v.validate_element(root, element("r", Group(element("a"), element("b"))))
    assert not outcome.valid
    report = ValidationReport()
    report_first_mismatch(outcome, report)
    payload = json.loads(report.to_json())
    assert payload["issues"][0]["location"] == {
        "source": "instance.xml",
        "path": [{"name": "r", "index": 1}, {"name": "wrong", "index": 1}],
        "attribute": None,
    }
    assert payload["issues"][0]["details"]["expected-elements"] == ["b"]


def test_reports_are_stable_across_repeated_constructions():
    payloads = []
    for _ in range(25):
        pattern = element(
            "r",
            Choice(
                Group(element("c"), element("b")),
                Interleave(element("a"), element("d")),
            ),
        )
        root = tree("<r><wrong/></r>")
        outcome = v.validate_element(root, pattern)
        assert not outcome.valid
        report = ValidationReport()
        report_first_mismatch(outcome, report)
        payloads.append(report.to_json())
    assert len(set(payloads)) == 1
    assert json.loads(payloads[0])["issues"][0]["details"]["expected-elements"] == [
        "a",
        "c",
        "d",
    ]


HASH_SCRIPT = r"""
import json
from xml.etree import ElementTree as ET

from pyxsd.relaxng.diagnostics import expectations, report_first_mismatch
from pyxsd.relaxng.names import Name, NameChoice
from pyxsd.relaxng.patterns import Attribute, Choice, Data, Element, Empty, Interleave, Text
from pyxsd.relaxng.datatypes import DatatypeContext, XsdDatatypeFactory
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element
from pyxsd.validation import ValidationReport

cx = DatatypeContext(None, {})
integer = XsdDatatypeFactory().create("integer", (), cx)
names = {Name(None, value) for value in ("c", "a", "b")}
elements = {Name(None, value) for value in ("z", "x", "y")}
pattern = Choice(
    Element(
        Name(None, "r"),
        Interleave(
            Element(NameChoice(tuple(names)), Empty()),
            Attribute(NameChoice(tuple(elements)), Data(integer)),
        ),
    ),
    Element(Name(None, "r"), Empty()),
)
root = RelaxNGElement.from_etree(ET.fromstring('<r a="bad"><z/></r>'))
outcome = validate_element(root, pattern)
report = ValidationReport()
report_first_mismatch(outcome, report)
summary = expectations(outcome.mismatch.preceding)
print(
    json.dumps(
        {
            "elements": list(summary.elements),
            "attributes": list(summary.attributes),
            "content": list(summary.content),
            "datatypes": list(summary.datatypes),
            "report": report.to_dict(),
        },
        sort_keys=True,
    )
)
"""


@pytest.mark.parametrize("seed", ["0", "1", "42"])
def test_projection_and_report_are_hash_seed_independent(seed):
    env = {**os.environ, "PYTHONHASHSEED": seed}
    first = subprocess.run(
        [sys.executable, "-c", HASH_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    second = subprocess.run(
        [sys.executable, "-c", HASH_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
    ).stdout
    assert first == second
