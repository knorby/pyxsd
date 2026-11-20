"""RELAX NG section 7 correctness restrictions on the composed graph.

Each restriction has a minimal incorrect schema and a near-neighbour correct
schema.  The checks run on the final composed graph: references are leaves in
the simplified schema tree, definitions are separate element-content roots,
and legal recursion closes through references rather than through Python
calls.
"""

from __future__ import annotations

import io

import pytest

from pyxsd.relaxng.checks import check_compiled_grammar
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeLibraryRegistry,
    DatatypeParameter,
)
from pyxsd.relaxng.names import Name, NameClass
from pyxsd.relaxng.origins import OriginTable
from pyxsd.relaxng.patterns import (
    Attribute,
    Element,
    Empty,
    Group,
    List,
    OneOrMore,
    Pattern,
    PatternPool,
    Text,
)
from pyxsd.relaxng.resources import ResourceLoader
from pyxsd.relaxng.simplify import CompiledGrammar, Definition, Simplifier, compose_grammars
from pyxsd.relaxng.syntax import read_full_syntax
from pyxsd.validation import ValidationReport

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def grammar(body: str) -> str:
    return f'<grammar xmlns="{NS}">{body}</grammar>'


def element(name: str, body: str = "<empty/>") -> str:
    return f'<element xmlns="{NS}" name="{name}">{body}</element>'


def data(type_name: str = "int") -> str:
    return f'<data type="{type_name}" datatypeLibrary="{XSD}"/>'


def compile_schema(xml: str, libraries: DatatypeLibraryRegistry | None = None):
    loader = ResourceLoader()
    resource = loader.load(io.StringIO(xml))
    report = ValidationReport()
    bundle = Simplifier(
        report,
        PatternPool(),
        libraries if libraries is not None else DatatypeLibraryRegistry(),
    ).simplify(read_full_syntax(resource))
    return compose_grammars(bundle, loader), report


def checked(xml: str, libraries: DatatypeLibraryRegistry | None = None):
    compiled, report = compile_schema(xml, libraries)
    assert not report.errors, report.errors
    check_compiled_grammar(compiled, report)
    assert all(issue.phase == "schema" for issue in report.errors)
    return compiled, report


def codes(report: ValidationReport) -> list[str]:
    return [issue.code for issue in report.errors]


def finding(report: ValidationReport, code: str):
    matches = [issue for issue in report.errors if issue.code == code]
    assert matches, codes(report)
    issue = matches[0]
    assert issue.phase == "schema"
    assert issue.location is not None
    return issue


def leaf_name(issue) -> str:
    assert issue.location is not None
    return issue.location.path[-1].name.rsplit("}", 1)[-1]


def synthetic(pattern: Pattern) -> CompiledGrammar:
    return CompiledGrammar(pattern, {}, PatternPool(), ())


# ---------------------------------------------------------------------------
# Section 7.1: contextual restrictions
# ---------------------------------------------------------------------------


def test_attribute_content_cannot_contain_attribute():
    _, report = checked(element("x", '<attribute name="a"><attribute name="b"/></attribute>'))

    issue = finding(report, "relaxng-attribute-in-attribute")
    steps = [step.name.rsplit("}", 1)[-1] for step in issue.location.path]
    assert steps[-2:] == ["attribute", "attribute"]


def test_sibling_attributes_in_element_content_are_correct():
    _, report = checked(element("x", '<group><attribute name="a"/><attribute name="b"/></group>'))
    assert not report.errors


def test_attribute_content_cannot_contain_reference():
    _, report = checked(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><element name="x"><attribute name="q"><ref name="b"/></attribute>'
            "</element></define>"
            '<define name="b"><element name="y"><empty/></element></define>'
        )
    )

    issue = finding(report, "relaxng-ref-in-attribute")
    assert leaf_name(issue) == "ref"


def test_reference_in_element_content_is_correct():
    _, report = checked(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><element name="x"><group><attribute name="q"><text/></attribute>'
            '<ref name="b"/></group></element></define>'
            '<define name="b"><element name="y"><empty/></element></define>'
        )
    )
    assert not report.errors


@pytest.mark.parametrize(
    ("repetition", "code"),
    [
        (
            '<group><attribute name="a"/><attribute name="b"/></group>',
            "relaxng-attribute-in-repeated-group",
        ),
        (
            '<interleave><attribute name="a"/><attribute name="b"/></interleave>',
            "relaxng-attribute-in-repeated-interleave",
        ),
    ],
)
def test_attributes_cannot_be_grouped_by_a_repetition(repetition, code):
    _, report = checked(element("x", f"<oneOrMore>{repetition}</oneOrMore>"))

    finding(report, code)


@pytest.mark.parametrize(
    "repetition",
    [
        '<attribute name="a"/>',
        '<choice><attribute name="a"/><attribute name="b"/></choice>',
        '<group><group><attribute name="a"/></group></group>',
        '<group><interleave><attribute name="a"/></interleave></group>',
    ],
)
def test_directly_repeated_attributes_are_correct(repetition):
    _, report = checked(element("x", f"<oneOrMore>{repetition}</oneOrMore>"))
    assert not report.errors


def test_a_group_above_a_repetition_is_correct():
    _, report = checked(
        element(
            "x",
            '<group><oneOrMore><attribute name="a"/></oneOrMore>'
            '<element name="y"><empty/></element></group>',
        )
    )
    assert not report.errors


def test_repeated_attribute_context_propagates_through_choices():
    _, report = checked(
        element(
            "x",
            '<oneOrMore><choice><group><attribute name="a"/><attribute name="b"/>'
            '</group><attribute name="c"/></choice></oneOrMore>',
        )
    )

    finding(report, "relaxng-attribute-in-repeated-group")


def test_list_cannot_contain_reference():
    _, report = checked(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><element name="x"><list><ref name="b"/></list></element>'
            "</define>"
            '<define name="b"><element name="y"><empty/></element></define>'
        )
    )

    issue = finding(report, "relaxng-list-contains-ref")
    assert leaf_name(issue) == "ref"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ('<list><data type="token"/></list>', "relaxng-list-contains-list"),
        ('<attribute name="a"/>', "relaxng-list-contains-attribute"),
        ("<text/>", "relaxng-list-contains-text"),
        (
            '<interleave><data type="token"/><data type="token"/></interleave>',
            "relaxng-list-contains-interleave",
        ),
    ],
)
def test_list_rejects_prohibited_content(content, code):
    _, report = checked(element("x", f"<list>{content}</list>"))

    finding(report, code)


@pytest.mark.parametrize(
    "content",
    [
        '<data type="token"/>',
        '<group><data type="token"/><data type="token"/></group>',
        '<oneOrMore><data type="token"/></oneOrMore>',
        "<empty/>",
        '<choice><empty/><data type="token"/></choice>',
    ],
)
def test_list_accepts_token_patterns(content):
    _, report = checked(element("x", f"<list>{content}</list>"))
    assert not report.errors


def test_data_except_cannot_contain_reference():
    _, report = checked(
        grammar(
            '<start><ref name="a"/></start><define name="a">'
            f'<element name="x"><data type="int" datatypeLibrary="{XSD}">'
            '<except><ref name="b"/></except></data></element>'
            "</define>"
            '<define name="b"><element name="y"><empty/></element></define>'
        )
    )

    issue = finding(report, "relaxng-except-contains-ref")
    assert leaf_name(issue) == "ref"


@pytest.mark.parametrize(
    ("excluded", "code"),
    [
        ('<attribute name="a"/>', "relaxng-except-contains-attribute"),
        ("<text/>", "relaxng-except-contains-text"),
        (
            '<list><data type="token"/></list>',
            "relaxng-except-contains-list",
        ),
        (
            f"<group>{data()}{data()}</group>",
            "relaxng-except-contains-group",
        ),
        (
            f"<interleave>{data()}{data()}</interleave>",
            "relaxng-except-contains-interleave",
        ),
        (
            f"<oneOrMore>{data()}</oneOrMore>",
            "relaxng-except-contains-one-or-more",
        ),
        ("<empty/>", "relaxng-except-contains-empty"),
    ],
)
def test_data_except_rejects_prohibited_content(excluded, code):
    _, report = checked(
        element(
            "x",
            f'<data type="int" datatypeLibrary="{XSD}"><except>{excluded}</except></data>',
        )
    )

    finding(report, code)


@pytest.mark.parametrize(
    "excluded",
    [
        data(),
        "<value>five</value>",
        f"<choice>{data()}<value>five</value></choice>",
        f'<data type="int" datatypeLibrary="{XSD}"><except><value>five</value></except></data>',
    ],
)
def test_data_except_accepts_data_value_and_choice(excluded):
    _, report = checked(
        element(
            "x",
            f'<data type="int" datatypeLibrary="{XSD}"><except>{excluded}</except></data>',
        )
    )
    assert not report.errors


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("<empty/>", "relaxng-start-contains-empty"),
        ("<text/>", "relaxng-start-contains-text"),
        ('<attribute name="a"/>', "relaxng-start-contains-attribute"),
        ('<data type="token"/>', "relaxng-start-contains-data"),
        (
            '<data type="token"><except><value>a</value></except></data>',
            "relaxng-start-contains-data",
        ),
        ("<value>v</value>", "relaxng-start-contains-value"),
        ("<list><text/></list>", "relaxng-start-contains-list"),
        (
            '<group><element name="x"><empty/></element><element name="y"><empty/></element>'
            "</group>",
            "relaxng-start-contains-group",
        ),
        (
            '<interleave><element name="x"><empty/></element><element name="y"><empty/></element>'
            "</interleave>",
            "relaxng-start-contains-interleave",
        ),
        (
            '<oneOrMore><element name="x"><empty/></element></oneOrMore>',
            "relaxng-start-contains-one-or-more",
        ),
        (
            '<choice><element name="x"><empty/></element><empty/></choice>',
            "relaxng-start-contains-empty",
        ),
    ],
)
def test_start_rejects_non_element_patterns(content, code):
    _, report = checked(grammar(f"<start>{content}</start>"))

    finding(report, code)


@pytest.mark.parametrize(
    "body",
    [
        f"<start>{element('x')}</start>",
        '<start><ref name="a"/></start><define name="a"><element name="x"><empty/></element>'
        "</define>",
        '<start><ref name="a"/></start><define name="a"><element name="x"><group>'
        '<element name="y"><empty/></element><element name="z"><empty/></element>'
        "</group></element></define>",
        '<start><choice><element name="x"><empty/></element><element name="y"><empty/></element>'
        "</choice></start>",
        "<start><notAllowed/></start>",
        f"<start><group><empty/>{element('x')}</group></start>",
    ],
)
def test_start_accepts_element_references_and_choices(body):
    _, report = checked(grammar(body))
    assert not report.errors


# ---------------------------------------------------------------------------
# Section 7.2: string sequences (content types)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "offender"),
    [
        (f"<group>{data()}{element('y')}</group>", "group"),
        (f"<group>{data()}{data()}</group>", "group"),
        (f"<group>{data()}<text/></group>", "group"),
        (f"<interleave>{data()}{data()}</interleave>", "interleave"),
        (f"<oneOrMore>{data()}</oneOrMore>", "oneOrMore"),
        (
            f'<attribute name="a"><group>{data()}{data()}</group></attribute>',
            "group",
        ),
        (
            f'<attribute name="a"><interleave>{data()}<text/></interleave></attribute>',
            "interleave",
        ),
    ],
)
def test_element_and_attribute_content_needs_a_content_type(content, offender):
    _, report = checked(element("x", content))

    issue = finding(report, "relaxng-content-type")
    assert leaf_name(issue) == offender
    assert "content type" in issue.message


@pytest.mark.parametrize(
    "content",
    [
        f"<choice>{data()}{element('y')}</choice>",
        f"<group>{element('y')}{element('z')}</group>",
        "<oneOrMore><text/></oneOrMore>",
        f"<group>{data()}<empty/></group>",
        "<notAllowed/>",
        f'<attribute name="a"><choice>{data()}<value>v</value></choice></attribute>',
        '<attribute name="a"><text/></attribute>',
        f"<list><group>{data()}{data()}</group></list>",
        f"<list><oneOrMore>{data()}</oneOrMore></list>",
    ],
)
def test_alternatives_and_list_content_are_correct(content):
    _, report = checked(element("x", content))
    assert not report.errors


def test_choices_of_string_and_element_patterns_are_correct():
    _, report = checked(
        element("x", f'<choice>{data()}<element name="y"><empty/></element></choice>')
    )
    assert not report.errors


@pytest.mark.parametrize(
    ("content", "offender"),
    [
        (f"<choice><group>{data()}{data()}</group><text/></choice>", "group"),
        (f"<choice>{data()}<group>{data()}{data()}</group></choice>", "group"),
        (f"<group><group>{data()}{data()}</group><text/></group>", "group"),
        (f"<group><text/><group>{data()}{data()}</group></group>", "group"),
        (f"<oneOrMore><group>{data()}{data()}</group></oneOrMore>", "group"),
    ],
)
def test_nested_content_type_failures_report_the_failing_pattern(content, offender):
    _, report = checked(element("x", content))

    issue = finding(report, "relaxng-content-type")
    assert leaf_name(issue) == offender


# ---------------------------------------------------------------------------
# Section 7.3: restrictions on attributes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "combinator",
    ["group", "interleave"],
)
def test_duplicate_attributes_are_rejected(combinator):
    _, report = checked(
        element(
            "x",
            f'<{combinator}><attribute name="a"/><attribute name="a"/></{combinator}>',
        )
    )

    issue = finding(report, "relaxng-duplicate-attribute")
    assert leaf_name(issue) == combinator


@pytest.mark.parametrize(
    "combinator",
    ["group", "interleave"],
)
def test_distinct_attributes_are_correct(combinator):
    _, report = checked(
        element(
            "x",
            f'<{combinator}><attribute name="a"/><attribute name="b"/></{combinator}>',
        )
    )
    assert not report.errors


def test_duplicate_attribute_through_a_nested_choice():
    _, report = checked(
        element(
            "x",
            '<group><choice><attribute name="a"/><attribute name="b"/></choice>'
            '<attribute name="a"/></group>',
        )
    )

    finding(report, "relaxng-duplicate-attribute")


def test_any_name_except_that_excludes_another_attribute_still_overlaps():
    _, report = checked(
        element(
            "x",
            '<interleave><attribute name="b"/><attribute><anyName><except><name>a</name>'
            "</except></anyName><text/></attribute></interleave>",
        )
    )

    finding(report, "relaxng-duplicate-attribute")


def test_any_name_except_can_exclude_the_other_attribute():
    _, report = checked(
        element(
            "x",
            '<interleave><attribute name="a"/><attribute><anyName><except><name>a</name>'
            "</except></anyName><text/></attribute></interleave>",
        )
    )

    assert "relaxng-duplicate-attribute" not in codes(report)


def test_namespace_exclusion_can_prevent_attribute_overlap():
    _, report = checked(
        element(
            "x",
            '<interleave><attribute xmlns:p="urn:one" name="p:a"/>'
            '<attribute><anyName><except><nsName ns="urn:one"/></except></anyName><text/>'
            "</attribute></interleave>",
        )
    )

    assert "relaxng-duplicate-attribute" not in codes(report)


def test_namespace_wildcards_overlap_attributes():
    _, report = checked(
        element(
            "x",
            '<interleave><attribute xmlns:p="urn:one" name="p:a"/>'
            '<attribute><nsName ns="urn:one"/><text/></attribute></interleave>',
        )
    )

    finding(report, "relaxng-duplicate-attribute")


def test_duplicate_attributes_through_an_expanded_reference():
    _, report = checked(
        grammar(
            '<start><ref name="root"/></start>'
            '<define name="root"><element name="x"><ref name="attrs"/></element></define>'
            '<define name="attrs"><group><attribute name="p"/><attribute name="p"/></group>'
            "</define>"
        )
    )

    finding(report, "relaxng-duplicate-attribute")


@pytest.mark.parametrize(
    "name_class",
    [
        "<attribute><anyName/><text/></attribute>",
        '<attribute><nsName ns="urn:one"/><text/></attribute>',
        "<attribute><anyName><except><name>a</name></except></anyName><text/></attribute>",
        '<attribute><choice><name>a</name><nsName ns="urn:one"/></choice><text/></attribute>',
    ],
)
def test_an_open_attribute_name_class_must_be_repeated(name_class):
    _, report = checked(element("x", name_class))

    issue = finding(report, "relaxng-unrepeated-attribute")
    steps = [step.name.rsplit("}", 1)[-1] for step in issue.location.path]
    assert "attribute" in steps


@pytest.mark.parametrize(
    "content",
    [
        '<attribute name="a"><text/></attribute>',
        "<attribute><choice><name>a</name><name>b</name></choice><text/></attribute>",
        "<oneOrMore><attribute><anyName/><text/></attribute></oneOrMore>",
        '<oneOrMore><attribute><nsName ns="urn:one"/><text/></attribute></oneOrMore>',
        "<oneOrMore><attribute><anyName><except><name>a</name></except></anyName><text/>"
        "</attribute></oneOrMore>",
    ],
)
def test_closed_or_repeated_attribute_name_classes_are_correct(content):
    _, report = checked(element("x", content))
    assert not report.errors


# ---------------------------------------------------------------------------
# Section 7.4: restrictions on interleave
# ---------------------------------------------------------------------------


def interleave_schema(*definitions: str) -> str:
    parts = [
        '<start><ref name="e0"/></start>',
        '<define name="e0"><element name="root"><interleave>',
    ]
    parts.extend(f'<ref name="e{index}"/>' for index in range(1, len(definitions) + 1))
    parts.append("</interleave></element></define>")
    for index, definition in enumerate(definitions, start=1):
        parts.append(f'<define name="e{index}">{definition}</define>')
    return grammar("".join(parts))


def wildcard_element(excluded: str) -> str:
    return f"<element><anyName><except><name>{excluded}</name></except></anyName><empty/></element>"


def test_interleave_rejects_overlapping_element_names():
    _, report = checked(interleave_schema(element("shared"), element("shared")))

    issue = finding(report, "relaxng-interleave-element-name")
    assert leaf_name(issue) == "interleave"


def test_interleave_accepts_distinct_element_names():
    _, report = checked(interleave_schema(element("one"), element("two")))
    assert not report.errors


def test_interleave_name_exclusions_are_set_semantic():
    _, report = checked(interleave_schema(wildcard_element("shared"), element("shared")))
    assert not report.errors


def test_interleave_wildcards_still_overlap_unexcluded_names():
    _, report = checked(interleave_schema(wildcard_element("shared"), element("other")))

    finding(report, "relaxng-interleave-element-name")


def test_interleave_namespace_exclusions_are_set_semantic():
    excluded = (
        '<element><anyName><except><nsName ns="urn:one"/></except></anyName><empty/></element>'
    )
    _, report = checked(
        interleave_schema(
            excluded,
            '<element><name ns="urn:one">x</name><empty/></element>',
        )
    )
    assert not report.errors


def test_interleave_namespace_exclusions_still_leave_other_namespaces():
    excluded = (
        '<element><anyName><except><nsName ns="urn:one"/></except></anyName><empty/></element>'
    )
    _, report = checked(
        interleave_schema(
            excluded,
            '<element><name ns="urn:two">x</name><empty/></element>',
        )
    )

    finding(report, "relaxng-interleave-element-name")


def test_interleave_namespace_wildcards_overlap_in_the_same_namespace():
    _, report = checked(
        interleave_schema(
            '<element><nsName ns="urn:one"/><empty/></element>',
            '<element><name ns="urn:one">x</name><empty/></element>',
        )
    )

    finding(report, "relaxng-interleave-element-name")


def test_interleave_namespace_wildcards_do_not_overlap_across_namespaces():
    _, report = checked(
        interleave_schema(
            '<element><nsName ns="urn:one"/><empty/></element>',
            '<element><name ns="urn:two">x</name><empty/></element>',
        )
    )
    assert not report.errors


def test_nested_interleave_checks_the_outer_operands():
    _, report = checked(
        grammar(
            '<start><ref name="e0"/></start>'
            '<define name="e0"><element name="root"><interleave>'
            '<interleave><ref name="e1"/><ref name="e2"/></interleave>'
            '<ref name="e3"/></interleave></element></define>'
            f'<define name="e1">{element("x")}</define>'
            f'<define name="e2">{element("y")}</define>'
            f'<define name="e3">{element("x")}</define>'
        )
    )

    issue = finding(report, "relaxng-interleave-element-name")
    steps = [step.name.rsplit("}", 1)[-1] for step in issue.location.path]
    assert steps[-1] == "interleave"


def test_interleave_rejects_text_in_both_operands():
    _, report = checked(element("x", "<interleave><text/><text/></interleave>"))

    issue = finding(report, "relaxng-interleave-text")
    assert leaf_name(issue) == "interleave"


def test_interleave_text_through_nested_combinators():
    _, report = checked(
        element(
            "x",
            "<interleave><choice><text/><empty/></choice><optional><text/></optional></interleave>",
        )
    )

    finding(report, "relaxng-interleave-text")


def test_interleave_accepts_text_on_one_side():
    _, report = checked(element("x", f"<interleave><text/>{element('y')}</interleave>"))
    assert not report.errors


def test_mixed_text_that_becomes_two_text_operands_is_rejected():
    _, report = checked(element("x", "<mixed><text/></mixed>"))

    finding(report, "relaxng-interleave-text")


def test_mixed_content_with_an_element_is_correct():
    _, report = checked(element("x", f"<mixed>{element('y')}</mixed>"))
    assert not report.errors


# ---------------------------------------------------------------------------
# Recursive graphs
# ---------------------------------------------------------------------------


def test_checks_terminate_on_a_legal_recursive_content_graph():
    _, report = checked(
        grammar(
            '<start><ref name="node"/></start>'
            '<define name="node"><element name="node"><choice>'
            f'{data()}<ref name="node"/></choice></element></define>'
        )
    )
    assert not report.errors


def test_duplicate_attributes_are_found_in_recursive_content():
    _, report = checked(
        grammar(
            '<start><ref name="node"/></start>'
            '<define name="node"><element name="node"><group>'
            '<attribute name="a"/><attribute name="a"/>'
            '<optional><ref name="node"/></optional>'
            "</group></element></define>"
        )
    )

    finding(report, "relaxng-duplicate-attribute")


def test_interleave_text_overlap_is_found_in_recursive_content():
    _, report = checked(
        grammar(
            '<start><ref name="node"/></start>'
            '<define name="node"><element name="node"><interleave><text/>'
            '<group><text/><optional><ref name="node"/></optional></group>'
            "</interleave></element></define>"
        )
    )

    finding(report, "relaxng-interleave-text")


# ---------------------------------------------------------------------------
# Checker contract
# ---------------------------------------------------------------------------


def test_check_does_not_mutate_the_compiled_graph():
    compiled, report = compile_schema(
        grammar(
            '<start><ref name="node"/></start>'
            '<define name="node"><element name="node"><group>'
            '<attribute name="a"/><attribute name="a"/>'
            '<optional><ref name="node"/></optional>'
            "</group></element></define>"
        )
    )
    start = compiled.start
    definitions = dict(compiled.definitions)
    size = len(compiled.graph)

    check_compiled_grammar(compiled, report)

    assert compiled.start is start
    assert dict(compiled.definitions) == definitions
    assert len(compiled.graph) == size


def test_repeated_checks_are_deterministic():
    compiled, _ = compile_schema(
        grammar(
            '<start><choice><ref name="a"/><ref name="b"/></choice></start>'
            '<define name="a"><element name="a"><group><attribute name="p"/>'
            '<attribute name="p"/></group></element></define>'
            '<define name="b"><element name="b"><group><attribute name="p"/>'
            '<attribute name="p"/></group></element></define>'
        )
    )
    first = ValidationReport()
    second = ValidationReport()

    check_compiled_grammar(compiled, first)
    check_compiled_grammar(compiled, second)

    def findings(report: ValidationReport):
        return [(issue.code, issue.phase, issue.location) for issue in report.errors]

    assert findings(first) == findings(second)
    assert len(findings(first)) == 1
    assert findings(first)[0][0] == "relaxng-duplicate-attribute"


def test_a_shared_interned_pattern_is_checked_once():
    _, report = checked(
        grammar(
            '<start><choice><ref name="a"/><ref name="b"/></choice></start>'
            '<define name="a"><element name="a"><group><attribute name="p"/>'
            '<attribute name="p"/></group></element></define>'
            '<define name="b"><element name="b"><group><attribute name="p"/>'
            '<attribute name="p"/></group></element></define>'
        )
    )

    assert codes(report).count("relaxng-duplicate-attribute") == 1


def test_a_shared_element_content_is_checked_once():
    shared = Element(Name(None, "x"), OneOrMore(List(Empty())))
    report = ValidationReport()
    compiled = CompiledGrammar(
        shared,
        {"a": Definition("a", shared, OriginTable())},
        PatternPool(),
        (),
    )

    check_compiled_grammar(compiled, report)

    assert codes(report) == ["relaxng-content-type"]


def test_findings_without_a_source_location_still_have_a_stable_code():
    report = ValidationReport()

    check_compiled_grammar(synthetic(Empty()), report)

    assert codes(report) == ["relaxng-start-contains-empty"]
    assert report.errors[0].phase == "schema"
    assert report.errors[0].location is None


class _SealedDatatype(Datatype):
    """A datatype whose operations must never be needed by graph checks."""

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        raise AssertionError("the correctness checker must not validate lexical values")

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        raise AssertionError("the correctness checker must not compare datatype values")


class _SealedFactory(DatatypeFactory):
    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        del type_name, parameters, context
        return _SealedDatatype()


def test_datatype_operations_are_not_schema_correctness_checks():
    libraries = DatatypeLibraryRegistry()
    libraries.register("urn:sealed", DatatypeLibraryRegistration(_SealedFactory(), "sealed/1"))
    _, report = checked(
        grammar(
            '<start><element name="x"><data type="thing" '
            'datatypeLibrary="urn:sealed"/></element></start>'
        ),
        libraries,
    )
    assert not report.errors


class _UnknownPattern(Pattern):
    """A pattern outside the documented graph contract."""


class _UnknownNameClass(NameClass):
    def contains(self, uri: str | None, local: str) -> bool:
        return False


def test_an_unknown_pattern_in_start_is_rejected():
    with pytest.raises(TypeError):
        check_compiled_grammar(synthetic(_UnknownPattern()), ValidationReport())


def test_an_unknown_pattern_below_a_context_violation_is_rejected():
    start = Group(Empty(), _UnknownPattern())
    with pytest.raises(TypeError):
        check_compiled_grammar(synthetic(start), ValidationReport())


def test_an_unknown_pattern_in_element_content_is_rejected():
    start = Element(Name(None, "x"), _UnknownPattern())
    with pytest.raises(TypeError):
        check_compiled_grammar(synthetic(start), ValidationReport())


def test_an_unknown_name_class_is_rejected():
    start = Element(Name(None, "x"), Attribute(_UnknownNameClass(), Text()))
    with pytest.raises(TypeError):
        check_compiled_grammar(synthetic(start), ValidationReport())
