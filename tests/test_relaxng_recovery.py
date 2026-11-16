"""Deterministic bounded RELAX NG recovery and report completeness."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from xml.etree import ElementTree as ET

import pytest

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.relaxng import RelaxNGLimits, RelaxNGSchema
from pyxsd.relaxng import validator as v
from pyxsd.relaxng.datatypes import (
    BuiltinDatatypeFactory,
    DatatypeContext,
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
    report_mismatches,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.names import AnyName, Name
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
    choice,
    one_or_more,
)
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.validation import ValidationReport

CX = DatatypeContext(None, {})
STRING = BuiltinDatatypeFactory().create("string", (), CX)
INTEGER = XsdDatatypeFactory().create("integer", (), CX)

NS = "http://relaxng.org/ns/structure/1.0"
SIMPLE_SCHEMA = (
    f'<grammar xmlns="{NS}"><start><element name="doc">'
    '<optional><element name="item"><text/></element></optional>'
    "</element></start></grammar>"
)
BROKEN_SCHEMA = f'<grammar xmlns="{NS}"/>'


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


def recover(xml, pattern, *, source="instance.xml", limits=None):
    outcome = v.validate_element_recovering(tree(xml, source), pattern, limits=limits)
    report = ValidationReport()
    report_mismatches(outcome, report, limits=limits)
    return outcome, report


def codes(report):
    return [issue.code for issue in report]


# ---------------------------------------------------------------------------
# Independent findings and deterministic order


def test_two_independent_sibling_subtree_errors_are_both_reported():
    outcome, report = recover("<r><first/><second/></r>", element("r"))

    assert not outcome.valid
    assert outcome.recovered is True
    assert outcome.complete is True
    assert codes(report) == [UNEXPECTED_ELEMENT, UNEXPECTED_ELEMENT]
    assert [issue.element for issue in report] == ["first", "second"]
    assert [issue.location for issue in report] == [
        IssueLocation("instance.xml", (PathStep("r", 1), PathStep("first", 1))),
        IssueLocation("instance.xml", (PathStep("r", 1), PathStep("second", 1))),
    ]
    assert report.diagnostics_complete is True
    assert report.error_count == 2


def test_multiple_invalid_attributes_report_each_occurrence_in_sorted_order():
    pattern = element(
        "r",
        Group(
            attribute("b", Data(INTEGER)),
            attribute("a", Data(INTEGER)),
        ),
    )
    outcome, report = recover('<r b="worse" a="bad"/>', pattern)

    assert not outcome.valid
    assert codes(report) == [INVALID_SCALAR, INVALID_SCALAR]
    assert [issue.location.attribute for issue in report] == ["a", "b"]
    assert all(issue.location.path == (PathStep("r", 1),) for issue in report)
    assert all(issue.details["recovery"] == "consumed-attribute" for issue in report)
    assert MISSING_ATTRIBUTE not in codes(report)
    assert report.diagnostics_complete is True


def test_missing_attribute_is_reported_before_a_recoverable_content_error():
    pattern = element("r", Group(attribute("flag"), Empty()))
    outcome, report = recover("<r>oops</r>", pattern)

    assert not outcome.valid
    assert codes(report) == [MISSING_ATTRIBUTE, UNEXPECTED_CONTENT]
    assert report.issues[0].location == IssueLocation("instance.xml", (PathStep("r", 1),))
    assert report.issues[0].location_precision == "container"
    assert report.issues[0].details["expected-attributes"] == ["flag"]
    assert report.issues[1].details["content-index"] == 0
    assert report.issues[1].details["actual-lexical"] == "oops"
    assert MISSING_CONTENT not in codes(report)
    assert report.diagnostics_complete is True


def test_recovery_suppresses_follow_on_missing_content():
    # Rejecting the only supplied text must not also report the expected child.
    _outcome, report = recover("<r>bad</r>", element("r", element("a")))
    assert codes(report) == [UNEXPECTED_CONTENT]

    # Rejecting an unexpected subtree must not also report the expected child.
    _outcome, report = recover("<r><wrong/></r>", element("r", element("a")))
    assert codes(report) == [UNEXPECTED_ELEMENT]

    # A rejected text after accepted content must not also report missing b.
    pattern = element("r", Group(element("a"), element("b")))
    _outcome, report = recover("<r><a/>bad</r>", pattern)
    assert codes(report) == [UNEXPECTED_CONTENT]

    # A genuinely missing sibling remains reportable.
    _outcome, report = recover("<r><a/></r>", pattern)
    assert codes(report) == [MISSING_CONTENT]
    assert report.issues[0].details["expected-elements"] == ["b"]


def test_ignored_subtrees_do_not_generate_descendant_findings():
    _outcome, report = recover('<r><wrong a="bad">text<deep/></wrong></r>', element("r"))

    assert codes(report) == [UNEXPECTED_ELEMENT]
    assert report.issues[0].element == "wrong"
    assert report.issues[0].location == IssueLocation(
        "instance.xml", (PathStep("r", 1), PathStep("wrong", 1))
    )


def test_recovery_keeps_weak_whitespace_matches_nonaccepting():
    outcome, report = recover("<r>   </r>", element("r", element("a")))
    assert not outcome.valid
    assert codes(report) == [MISSING_CONTENT]
    assert outcome.recovered is True


def test_recovery_continues_after_scalar_failures_in_sibling_subtrees():
    pattern = element("r", OneOrMore(element("n", Data(INTEGER))))
    outcome, report = recover("<r><n>bad</n><n>worse</n></r>", pattern)

    assert not outcome.valid
    assert codes(report) == [INVALID_SCALAR, INVALID_SCALAR]
    assert [issue.location for issue in report] == [
        IssueLocation("instance.xml", (PathStep("r", 1), PathStep("n", 1))),
        IssueLocation("instance.xml", (PathStep("r", 1), PathStep("n", 2))),
    ]
    assert MISSING_CONTENT not in codes(report)
    assert report.diagnostics_complete is True


def test_invalid_attribute_does_not_duplicate_a_missing_attribute():
    pattern = element(
        "r",
        Group(attribute("a", Data(INTEGER)), attribute("b")),
    )
    _outcome, report = recover('<r a="bad"/>', pattern)

    assert codes(report) == [INVALID_SCALAR, MISSING_ATTRIBUTE]
    assert report.issues[1].details["expected-attributes"] == ["b"]


def test_unexpected_and_invalid_attribute_recovery_are_distinguished():
    _outcome, report = recover('<r a="1"/>', element("r"))
    assert codes(report) == [UNEXPECTED_ATTRIBUTE]
    assert report.issues[0].details["actual-name"] == "a"

    _outcome, report = recover('<r a="bad"/>', element("r", Attribute(AnyName(), Data(INTEGER))))
    assert codes(report) == [INVALID_SCALAR]
    assert report.issues[0].details["expected-attributes"] == ["*"]
    assert report.issues[0].details["expected-datatypes"] == ["xsd:integer"]


# ---------------------------------------------------------------------------
# Recovery is never acceptance


def test_recovery_states_never_produce_a_valid_verdict():
    cases = [
        ("<r><extra/></r>", element("r")),
        ('<r a="bad"/>', element("r", attribute("a", Data(INTEGER)))),
        ("<r>bad</r>", element("r", Data(INTEGER))),
        ("<r/>", element("r", attribute("a"))),
        ("<r><n>bad</n></r>", element("r", OneOrMore(element("n", Data(INTEGER))))),
    ]
    for xml, pattern in cases:
        outcome, report = recover(xml, pattern)
        assert not outcome.valid, xml
        assert outcome.mismatches, xml
        assert outcome.recovered is True, xml
        assert report.has_errors, xml
        assert report.diagnostics_complete is True, xml


def test_recovery_retains_viable_states_and_the_first_fail_fast_mismatch():
    pattern = element("r", Group(element("a"), element("b")))
    root = tree("<r><a/><wrong/><b/></r>")

    fast = v.validate_element(root, pattern)
    outcome = v.validate_element_recovering(root, pattern)

    assert not fast.valid and not outcome.valid
    assert outcome.mismatches[0].event == fast.mismatch.event
    assert outcome.mismatches[0].element is fast.mismatch.element
    assert outcome.mismatches[0].name == fast.mismatch.name
    assert outcome.mismatches[0].preceding == fast.mismatch.preceding
    assert outcome.mismatches[0].recovery == "ignored-subtree"
    assert fast.mismatch.recovery is None
    assert outcome.residuals
    assert outcome.recovered is True
    assert outcome.complete is True


def test_recovery_root_mismatch_reports_one_unexpected_element():
    outcome, report = recover("<wrong/>", element("r"))
    assert not outcome.valid
    assert outcome.recovered is True
    assert codes(report) == [UNEXPECTED_ELEMENT]
    assert report.issues[0].element == "wrong"
    assert report.diagnostics_complete is True


def test_structural_failure_is_one_complete_finding_without_recovery():
    root = RelaxNGElement.from_etree(ET.Comment("not an element"))
    outcome = v.validate_element_recovering(root, element("r"))
    report = ValidationReport()
    report_mismatches(outcome, report)

    assert not outcome.valid
    assert outcome.recovered is False
    assert outcome.complete is True
    assert codes(report) == [INVALID_STRUCTURE]
    assert report.issues[0].location is None


def test_missing_top_level_content_uses_document_end_without_recovery():
    later = element("later")
    outcome = v.validate_element_recovering(tree("<r/>"), Group(element("r"), later))
    report = ValidationReport()
    report_mismatches(outcome, report)

    assert not outcome.valid
    assert outcome.recovered is False
    assert outcome.mismatches[0].event == "document_end"
    assert outcome.residuals == (later,)
    assert codes(report) == [MISSING_CONTENT]


# ---------------------------------------------------------------------------
# Report completeness and retention


def test_zero_retention_keeps_counts_and_marks_the_report_incomplete():
    outcome = v.validate_element_recovering(tree("<r><first/><second/></r>"), element("r"))
    limited = ValidationReport(max_issues=0)
    report_mismatches(outcome, limited)

    assert limited.issues == []
    assert limited.has_errors
    assert limited.error_count == 2
    assert limited.suppressed_count == 2
    assert limited.diagnostics_complete is False
    payload = limited.to_dict()
    assert payload["counts"] == {"error": 2, "warning": 0}
    assert payload["suppressed_count"] == 2
    assert payload["diagnostics_complete"] is False
    assert json.loads(limited.to_json())["diagnostics_complete"] is False


def test_partial_retention_retains_the_first_finding_and_stays_incomplete():
    outcome, _report = recover("<r><first/><second/></r>", element("r"))

    partial = ValidationReport(max_issues=1)
    report_mismatches(outcome, partial)
    assert codes(partial) == [UNEXPECTED_ELEMENT]
    assert partial.error_count == 2
    assert partial.suppressed_count == 1
    assert partial.diagnostics_complete is False

    limited = ValidationReport()
    report_mismatches(outcome, limited, limits=RelaxNGLimits(max_diagnostics=1))
    assert codes(limited) == [UNEXPECTED_ELEMENT]
    assert limited.error_count == 2
    assert limited.suppressed_count == 1
    assert limited.diagnostics_complete is False

    tighter = ValidationReport(max_issues=0)
    report_mismatches(outcome, tighter, limits=RelaxNGLimits(max_diagnostics=5))
    assert tighter.issues == []
    assert tighter.suppressed_count == 2
    assert tighter.diagnostics_complete is False


def test_completeness_does_not_upgrade_an_unspecified_or_incomplete_report():
    outcome = v.validate_element_recovering(tree("<r/>"), element("r"))
    report = ValidationReport(diagnostics_complete=False)
    report_mismatches(outcome, report)
    assert report.diagnostics_complete is False

    mismatch = v.ValidationMismatch(
        "start_tag_open", tree("<r/>"), element("r"), "wrong", recovery="ignored-subtree"
    )
    interrupted = v.RecoveryOutcome(
        False, NotAllowed(), (mismatch,), recovered=True, complete=False
    )
    partial = ValidationReport()
    report_mismatches(interrupted, partial)
    assert partial.error_count == 1
    assert partial.diagnostics_complete is False


def test_invalid_recovery_outcome_requires_findings():
    outcome = v.RecoveryOutcome(False, NotAllowed())
    with pytest.raises(ValueError, match="findings"):
        report_mismatches(outcome, ValidationReport())


def test_recovery_uses_limits_for_retention_not_worked_around_them():
    outcome, report = recover("<r><first/><second/></r>", element("r"))
    assert codes(report) == [UNEXPECTED_ELEMENT, UNEXPECTED_ELEMENT]

    limited = ValidationReport()
    report_mismatches(outcome, limited, limits=RelaxNGLimits(max_diagnostics=2))
    assert len(limited) == 2
    assert limited.suppressed_count == 0
    assert limited.diagnostics_complete is True


def test_derivative_and_memo_budgets_raise_instead_of_recovering():
    for budget in ("max_derivative_states", "max_memo_entries"):
        with pytest.raises(RelaxNGLimitError) as raised:
            v.validate_element_recovering(
                tree("<r><wrong/></r>"),
                element("r"),
                limits=RelaxNGLimits(**{budget: 0}),
            )
        assert raised.value.stage == "validate"
        assert raised.value.budget == budget

    # A diagnostic retention cap is not a work budget and never raises.
    outcome, report = recover(
        "<r><first/><second/></r>",
        element("r"),
        limits=RelaxNGLimits(max_diagnostics=0),
    )
    assert not outcome.valid
    assert report.has_errors
    assert report.diagnostics_complete is False


# ---------------------------------------------------------------------------
# Determinism


def test_recovery_report_order_and_json_are_stable_across_repetitions():
    pattern = element(
        "r",
        Group(
            attribute("b", Data(INTEGER)),
            Group(attribute("a", Data(INTEGER)), Empty()),
        ),
    )
    payloads = []
    for _ in range(25):
        _outcome, report = recover('<r b="worse" a="bad"><extra/></r>', pattern)
        assert codes(report) == [INVALID_SCALAR, INVALID_SCALAR, UNEXPECTED_ELEMENT]
        payloads.append(report.to_json())
    assert len(set(payloads)) == 1


RECOVERY_HASH_SCRIPT = r"""
import json
from xml.etree import ElementTree as ET

from pyxsd.locations import IssueLocation
from pyxsd.relaxng.datatypes import DatatypeContext, XsdDatatypeFactory
from pyxsd.relaxng.diagnostics import report_mismatches
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import Attribute, Data, Element, Empty, Group
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element_recovering
from pyxsd.validation import ValidationReport

cx = DatatypeContext(None, {})
integer = XsdDatatypeFactory().create("integer", (), cx)
pattern = Element(
    Name(None, "r"),
    Group(
        Attribute(Name(None, "b"), Data(integer)),
        Group(Attribute(Name(None, "a"), Data(integer)), Empty()),
    ),
)
root = RelaxNGElement.from_etree(
    ET.fromstring('<r b="worse" a="bad"><extra/></r>'),
    source=IssueLocation("instance.xml", ()),
)
outcome = validate_element_recovering(root, pattern)
report = ValidationReport()
report_mismatches(outcome, report)
print(report.to_json())
"""


@pytest.mark.parametrize("seed", ["0", "42"])
def test_recovery_report_is_hash_seed_independent(seed):
    first = subprocess.run(
        [sys.executable, "-c", RECOVERY_HASH_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    ).stdout
    second = subprocess.run(
        [sys.executable, "-c", RECOVERY_HASH_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
    ).stdout
    assert first == second


# ---------------------------------------------------------------------------
# Recovery transforms and continuity


def test_without_attributes_projects_every_current_position_constructor():
    a = attribute("a")
    b = attribute("b")

    assert v._without_attributes(a) == Empty()
    assert v._without_attributes(Group(a, b)) == Empty()
    assert v._without_attributes(Choice(a, element("n"))) == choice(Empty(), element("n"))
    assert v._without_attributes(Interleave(a, element("n"))) == element("n")
    assert v._without_attributes(OneOrMore(a)) == Empty()
    assert v._without_attributes(v._After(Group(a, element("n")), element("tail"))) == v._After(
        element("n"), element("tail")
    )
    ref = Ref()
    ref.bind(Group(a, Text()))
    assert v._without_attributes(ref) == Text()
    cyclic = Ref()
    cyclic.bind(Group(Empty(), cyclic))
    assert v._without_attributes(cyclic) == Empty()
    assert v._without_attributes(element("n", attribute("a"))) == element("n", attribute("a"))
    assert v._without_attributes(Data(INTEGER)) == Data(INTEGER)

    class Unknown(Pattern):
        pass

    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        v._without_attributes(Unknown())


def test_consume_attribute_only_removes_the_failed_name():
    a = attribute("a")
    b = attribute("b")
    tail = element("tail")

    assert v._consume_attribute(v._After(Group(a, b), tail), None, "a") == v._After(b, tail)
    assert v._consume_attribute(Attribute(AnyName(), Text()), None, "anything") == Empty()
    assert v._consume_attribute(Choice(a, b), None, "a") == choice(Empty(), b)
    assert v._consume_attribute(Interleave(a, b), None, "b") == a
    assert v._consume_attribute(OneOrMore(a), None, "a") == Empty()
    assert v._consume_attribute(Data(INTEGER), None, "a") == Data(INTEGER)
    assert v._consume_attribute(Text(), None, "a") == Text()
    ref = Ref()
    ref.bind(Group(a, b))
    assert v._consume_attribute(ref, None, "b") == a
    cyclic = Ref()
    cyclic.bind(Group(a, cyclic))
    assert v._consume_attribute(cyclic, None, "a") == Empty()
    assert v._consume_attribute(element("n", a), None, "a") == element("n", a)

    class Unknown(Pattern):
        pass

    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        v._consume_attribute(Unknown(), None, "a")


def test_relax_scalars_replaces_only_current_position_scalars():
    assert v._relax_scalars(Data(INTEGER)) == Text()
    assert v._relax_scalars(DataExcept(INTEGER, Value(INTEGER, "3"))) == Text()
    assert v._relax_scalars(Value(STRING, "x")) == Text()
    assert v._relax_scalars(List(Data(INTEGER))) == Text()
    assert v._relax_scalars(
        v._After(Group(Data(INTEGER), element("n")), element("tail"))
    ) == v._After(Group(Text(), element("n")), element("tail"))
    assert v._relax_scalars(Choice(Data(INTEGER), Text())) == choice(Text(), Text())
    assert v._relax_scalars(Interleave(Data(INTEGER), element("n"))) == Interleave(
        Text(), element("n")
    )
    assert v._relax_scalars(OneOrMore(Data(INTEGER))) == one_or_more(Text())
    ref = Ref()
    ref.bind(Data(INTEGER))
    assert v._relax_scalars(ref) == Text()
    cyclic = Ref()
    cyclic.bind(Choice(Empty(), cyclic))
    assert v._relax_scalars(cyclic) == Empty()
    assert v._relax_scalars(element("n", Data(INTEGER))) == element("n", Data(INTEGER))
    assert v._relax_scalars(attribute("a", Data(INTEGER))) == attribute("a", Data(INTEGER))
    assert v._relax_scalars(Text()) == Text()
    assert v._relax_scalars(Empty()) == Empty()
    assert v._relax_scalars(NotAllowed()) == NotAllowed()

    class Unknown(Pattern):
        pass

    with pytest.raises(TypeError, match="unknown RELAX NG pattern"):
        v._relax_scalars(Unknown())


def test_pop_continuations_keeps_viable_continuation_states():
    next_element = element("next")
    other = element("other")
    assert v._pop_continuations(v._After(Text(), next_element)) == next_element
    assert v._pop_continuations(
        Choice(v._After(Text(), next_element), v._After(Text(), other))
    ) == choice(next_element, other)
    ref = Ref()
    ref.bind(v._After(Text(), next_element))
    assert v._pop_continuations(ref) == next_element
    cyclic = Ref()
    cyclic.bind(v._After(Empty(), cyclic))
    assert v._pop_continuations(cyclic) is cyclic
    revisit = Ref()
    revisit.bind(Choice(Empty(), revisit))
    assert v._pop_continuations(revisit) == Empty()
    assert v._pop_continuations(Empty()) == Empty()
    assert v._pop_continuations(NotAllowed()) == Empty()
    assert v._pop_continuations(Data(INTEGER)) == Empty()


# ---------------------------------------------------------------------------
# Public document reports


def test_relaxng_schema_and_document_reports_carry_boolean_completeness():
    schema = RelaxNGSchema.compile(io.StringIO(SIMPLE_SCHEMA))
    assert schema.report.diagnostics_complete is True

    valid = schema.parse(io.StringIO("<doc/>"), typed=False)
    assert valid.report.diagnostics_complete is True
    assert json.loads(valid.report.to_json())["diagnostics_complete"] is True

    invalid = schema.parse(io.StringIO("<doc><wrong/></doc>"), typed=False)
    assert not invalid.is_valid
    assert codes(invalid.report) == [UNEXPECTED_ELEMENT]
    assert invalid.report.diagnostics_complete is True
    assert json.loads(invalid.report.to_json())["diagnostics_complete"] is True


def test_relaxng_instance_retention_cap_marks_the_document_report_incomplete():
    schema = RelaxNGSchema.compile(io.StringIO(SIMPLE_SCHEMA))
    document = schema.parse(
        io.StringIO("<doc><wrong/></doc>"),
        typed=False,
        limits=RelaxNGLimits(max_diagnostics=0),
    )

    assert not document.is_valid
    assert document.report.has_errors
    assert document.report.issues == []
    assert document.report.error_count == 1
    assert document.report.suppressed_count == 1
    assert document.report.diagnostics_complete is False
    payload = json.loads(document.report.to_json())
    assert payload["issues"] == []
    assert payload["diagnostics_complete"] is False


def test_relaxng_schema_retention_cap_marks_the_schema_report_incomplete():
    schema = RelaxNGSchema.compile(
        io.StringIO(BROKEN_SCHEMA), limits=RelaxNGLimits(max_diagnostics=0)
    )

    assert schema.report.has_errors
    assert schema.report.issues == []
    assert schema.report.suppressed_count > 0
    assert schema.report.diagnostics_complete is False
    assert json.loads(schema.report.to_json())["diagnostics_complete"] is False


def test_policy_declared_incomplete_reports_are_never_upgraded():
    from pyxsd.namespaces import parse_with_namespaces
    from pyxsd.resource_policy import ResourceData

    class IncompletePolicy:
        def __init__(self):
            self.reports = []

        def read(self, source, *, base, purpose):
            del base
            if purpose == "instance":
                return ResourceData("instance.xml", b"<doc/>")
            return ResourceData("schema.rng", SIMPLE_SCHEMA.encode("utf-8"))

        def parse(self, data, context):
            return parse_with_namespaces(io.BytesIO(data.content), context)

        def new_report(self):
            report = ValidationReport(diagnostics_complete=False)
            self.reports.append(report)
            return report

    policy = IncompletePolicy()
    schema = RelaxNGSchema.compile(io.StringIO(SIMPLE_SCHEMA), resource_policy=policy)
    assert schema.report.diagnostics_complete is False
    document = schema.parse(io.StringIO("<doc/>"), typed=False)
    assert document.report.diagnostics_complete is False
    assert document.schema.report.diagnostics_complete is False
    assert len(policy.reports) == 3
    assert all(report.diagnostics_complete is False for report in policy.reports)
