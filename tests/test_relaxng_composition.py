"""Grammar composition, resource closure, and guarded cyclic pattern graphs."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeLibraryRegistry,
    UnsupportedDatatypeError,
    XsdDatatypeFactory,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    Element,
    Empty,
    Interleave,
    NotAllowed,
    Pattern,
    PatternPool,
    Ref,
    nullable,
)
from pyxsd.relaxng.resources import ResourceLoader
from pyxsd.relaxng.simplify import (
    LocalGrammar,
    Simplifier,
    UnresolvedReference,
    compose_grammars,
)
from pyxsd.relaxng.syntax import read_full_syntax
from pyxsd.validation import ValidationReport

NS = "http://relaxng.org/ns/structure/1.0"


def grammar(body: str) -> str:
    return f'<grammar xmlns="{NS}">{body}</grammar>'


def element(name: str, body: str = "<empty/>") -> str:
    return f'<element name="{name}">{body}</element>'


def run(xml: str, *, path: Path | None = None, libraries=None):
    loader = ResourceLoader()
    if path is not None:
        path.write_text(xml, encoding="utf-8")
        resource = loader.load(path)
    else:
        resource = loader.load(io.StringIO(xml))
    report = ValidationReport()
    bundle = Simplifier(
        report, PatternPool(), libraries if libraries is not None else DatatypeLibraryRegistry()
    ).simplify(read_full_syntax(resource))
    return compose_grammars(bundle, loader), report


def deref(pattern: Pattern) -> Pattern:
    while isinstance(pattern, Ref):
        assert not isinstance(pattern, (UnresolvedReference, LocalGrammar))
        pattern = pattern.target
    return pattern


def codes(report):
    assert all(issue.phase == "schema" for issue in report.errors)
    return {issue.code for issue in report.errors}


def test_forward_reference_and_unreachable_definitions():
    compiled, report = run(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="unused"><ref name="unused"/></define>'
            f'<define name="a">{element("root")}</define>'
        )
    )
    assert not report.errors
    assert deref(compiled.start) == Element(Name(None, "root"), Empty())
    assert len(compiled.definitions) == 1
    assert all(isinstance(item.pattern, Element) for item in compiled.definitions.values())
    # Only the bound edge, element, and empty content belong to the graph.
    assert len(compiled.graph) == 3


@pytest.mark.parametrize("recursive", [False, True])
def test_long_alias_chain_expands_without_using_the_python_call_stack(recursive):
    count = 1100
    aliases = "".join(
        f'<define name="a{index}"><ref name="a{index + 1}"/></define>' for index in range(count)
    )
    content = '<optional><ref name="a0"/></optional>' if recursive else "<empty/>"
    compiled, report = run(
        grammar(
            '<start><ref name="a0"/></start>'
            + aliases
            + f'<define name="a{count}">{element("root", content)}</define>'
        )
    )

    assert not report.errors
    root = deref(compiled.start)
    assert isinstance(root, Element)
    assert root.name_class == Name(None, "root")
    assert len(compiled.definitions) == 1
    if recursive:
        assert isinstance(root.pattern, Choice)
        assert nullable(root.pattern)
        back = root.pattern.second if isinstance(root.pattern.first, Empty) else root.pattern.first
        assert deref(back) is root
    else:
        assert root.pattern == Empty()
    define_indexes = {
        step.index
        for origin in compiled.graph.origins(root)
        if origin.location is not None
        for step in origin.location.path
        if step.name == f"{{{NS}}}define"
    }
    assert define_indexes == set(range(1, count + 2))


@pytest.mark.parametrize("method,kind", [("choice", Choice), ("interleave", Interleave)])
@pytest.mark.parametrize("explicit_first", [False, True])
def test_combine_definitions(method, kind, explicit_first):
    first_combine = f' combine="{method}"' if explicit_first else ""
    compiled, report = run(
        grammar(
            '<start><ref name="a"/></start>'
            f'<define name="a"{first_combine}>{element("one")}</define>'
            f'<define name="a" combine="{method}">{element("two")}</define>'
        )
    )
    assert not report.errors
    result = deref(compiled.start)
    assert isinstance(result, kind)
    assert {deref(result.first).name_class.local, deref(result.second).name_class.local} == {
        "one",
        "two",
    }


@pytest.mark.parametrize(
    "body,code",
    [
        ("<start><empty/></start><start><text/></start>", "relaxng-duplicate-start"),
        (
            '<start><empty/></start><define name="a"><empty/></define>'
            '<define name="a"><text/></define>',
            "relaxng-duplicate-define",
        ),
        ('<start combine="wrong"><empty/></start>', "relaxng-invalid-combine"),
        (
            '<start combine="choice"><empty/></start><start combine="interleave"><text/></start>',
            "relaxng-conflicting-combine",
        ),
        ('<define name="a"><empty/></define>', "relaxng-missing-start"),
        ('<start><ref name="missing"/></start>', "relaxng-undefined-reference"),
        ('<start><parentRef name="a"/></start>', "relaxng-parent-reference"),
        (
            '<start><empty/></start><define name="a"><ref name="missing"/></define>',
            "relaxng-undefined-reference",
        ),
    ],
)
def test_scope_errors_even_in_unreachable_definitions(body, code):
    _, report = run(grammar(body))
    assert code in codes(report)
    assert all(issue.location is not None for issue in report.errors)


def test_nested_grammar_parent_reference_and_shadowing():
    compiled, report = run(
        grammar(
            '<start><grammar><start><parentRef name="a"/></start>'
            f'<define name="a">{element("inner")}</define></grammar></start>'
            f'<define name="a">{element("outer")}</define>'
        )
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "outer")
    assert len(compiled.definitions) == 1


@pytest.mark.parametrize(
    "definitions",
    [
        '<define name="a"><ref name="a"/></define>',
        '<define name="a"><ref name="b"/></define><define name="b"><ref name="a"/></define>',
        '<define name="a"><choice><empty/><ref name="a"/></choice></define>',
    ],
)
def test_unguarded_recursion_is_a_schema_error(definitions):
    compiled, report = run(grammar('<start><ref name="a"/></start>' + definitions))
    assert "relaxng-recursive-reference" in codes(report)
    assert isinstance(deref(compiled.start), (NotAllowed, Empty))


def test_guarded_mutual_recursion_is_bound_and_finite():
    compiled, report = run(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><element name="a"><ref name="b"/></element></define>'
            '<define name="b"><element name="b"><optional><ref name="a"/></optional>'
            "</element></define>"
        )
    )
    assert not report.errors
    a = deref(compiled.start)
    b = deref(a.pattern)
    assert isinstance(a, Element) and isinstance(b, Element)
    assert isinstance(b.pattern, Choice)
    assert nullable(b.pattern)
    back = b.pattern.second if isinstance(b.pattern.first, Empty) else b.pattern.first
    assert deref(back) is a
    assert len(compiled.definitions) == 2


def test_recursion_through_a_non_element_definition_is_guarded():
    compiled, report = run(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><choice><empty/><element name="node">'
            '<ref name="a"/></element></choice></define>'
        )
    )
    assert not report.errors
    start = deref(compiled.start)
    assert isinstance(start, Choice)
    node = deref(start.second if isinstance(start.first, Empty) else start.first)
    assert node.pattern is start
    assert len(compiled.definitions) == 1


def test_nested_includes_rehome_references_and_apply_overrides(tmp_path):
    (tmp_path / "base.rng").write_text(
        grammar(f'<start><ref name="a"/></start><define name="a">{element("old")}</define>'),
        encoding="utf-8",
    )
    (tmp_path / "middle.rng").write_text(grammar('<include href="base.rng"/>'), encoding="utf-8")
    compiled, report = run(
        grammar(f'<include href="middle.rng"><define name="a">{element("new")}</define></include>'),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "new")
    assert len(compiled.dependencies) == 3
    origins = compiled.graph.origins(deref(compiled.start))
    assert {Path(origin.source_id).name for origin in origins} == {
        "root.rng",
        "middle.rng",
        "base.rng",
    }


def test_include_start_override(tmp_path):
    (tmp_path / "base.rng").write_text(
        grammar(f"<start>{element('old')}</start>"), encoding="utf-8"
    )
    compiled, report = run(
        grammar(f'<include href="base.rng"><start>{element("new")}</start></include>'),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "new")


@pytest.mark.parametrize(
    "included,override,code",
    [
        (
            grammar("<start><empty/></start>"),
            '<define name="absent"><empty/></define>',
            "relaxng-include-override",
        ),
        (
            grammar('<define name="a"><empty/></define>'),
            "<start><empty/></start>",
            "relaxng-include-override",
        ),
        (f'<empty xmlns="{NS}"/>', "", "relaxng-include-not-grammar"),
    ],
)
def test_invalid_include(tmp_path, included, override, code):
    (tmp_path / "base.rng").write_text(included, encoding="utf-8")
    _, report = run(
        grammar(f'<include href="base.rng">{override}</include>'), path=tmp_path / "root.rng"
    )
    assert code in codes(report)


@pytest.mark.parametrize("kind", ["include", "externalRef"])
def test_resource_cycles_are_schema_errors(tmp_path, kind):
    def resource(href):
        pattern = f'<{kind} href="{href}"/>'
        return grammar(pattern if kind == "include" else f"<start>{pattern}</start>")

    (tmp_path / "other.rng").write_text(resource("root.rng"), encoding="utf-8")
    _, report = run(resource("other.rng"), path=tmp_path / "root.rng")
    assert "relaxng-resource-cycle" in codes(report)


def test_two_authored_paths_one_resource_and_xml_base(tmp_path):
    directory = tmp_path / "schemas"
    directory.mkdir()
    (directory / "external.rng").write_text(
        f'<element xmlns="{NS}" name="external"><empty/></element>', encoding="utf-8"
    )
    compiled, report = run(
        grammar(
            '<start><choice><externalRef xml:base="schemas/" href="external.rng"/>'
            '<externalRef href="schemas/../schemas/external.rng"/></choice></start>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert len(compiled.dependencies) == 2
    dep = next(d for d in compiled.dependencies if d.references)
    assert len(dep.references) == 2
    assert deref(compiled.start).name_class == Name(None, "external")


def test_nested_percent_encoded_effective_base_resolves_once(tmp_path):
    correct_directory = tmp_path / "parts%20" / "sub "
    correct_directory.mkdir(parents=True)
    correct = correct_directory / "child.rng"
    correct.write_text(f'<element xmlns="{NS}" name="correct"><empty/></element>', encoding="utf-8")
    decoy_directory = tmp_path / "parts " / "sub "
    decoy_directory.mkdir(parents=True)
    decoy = decoy_directory / "child.rng"
    decoy.write_text(f'<element xmlns="{NS}" name="decoy"><empty/></element>', encoding="utf-8")

    compiled, report = run(
        grammar(
            '<start xml:base="parts%2520/">'
            '<externalRef xml:base="sub%20/" href="child.rng"/>'
            "</start>"
        ),
        path=tmp_path / "root.rng",
    )

    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "correct")
    dependency_ids = {dependency.canonical_id for dependency in compiled.dependencies}
    assert str(correct.resolve()) in dependency_ids
    assert str(decoy.resolve()) not in dependency_ids


@pytest.mark.parametrize("kind", ["include", "externalRef"])
def test_fragment_with_dot_segments_is_rejected_for_both_references(tmp_path, kind):
    target = tmp_path / "target.rng"
    target.write_text(grammar("<start><empty/></start>"), encoding="utf-8")
    href = "child.rng#fragment/../target.rng"
    body = (
        f'<include href="{href}"/>'
        if kind == "include"
        else f'<start><externalRef href="{href}"/></start>'
    )

    compiled, report = run(grammar(body), path=tmp_path / "root.rng")

    assert "relaxng-resource-read" in codes(report)
    assert [dependency.canonical_id for dependency in compiled.dependencies] == [
        str((tmp_path / "root.rng").resolve())
    ]


@pytest.mark.parametrize("kind", ["include", "externalRef"])
def test_nested_effective_base_fragment_is_rejected_before_normalization(tmp_path, kind):
    double_decoded = tmp_path / "parts " / "target.rng"
    double_decoded.parent.mkdir()
    double_decoded.write_text(grammar("<start><empty/></start>"), encoding="utf-8")
    once_decoded = tmp_path / "parts%20" / "target.rng"
    once_decoded.parent.mkdir()
    once_decoded.write_text(grammar("<start><empty/></start>"), encoding="utf-8")
    href = "child.rng#fragment/../target.rng"
    reference = f'<{kind} xml:base="parts%2520/" href="{href}"/>'
    body = reference if kind == "include" else f"<start>{reference}</start>"

    compiled, report = run(grammar(body), path=tmp_path / "root.rng")

    assert "relaxng-resource-read" in codes(report)
    assert [dependency.canonical_id for dependency in compiled.dependencies] == [
        str((tmp_path / "root.rng").resolve())
    ]
    assert str(double_decoded.resolve()) not in {
        dependency.canonical_id for dependency in compiled.dependencies
    }
    assert str(once_decoded.resolve()) not in {
        dependency.canonical_id for dependency in compiled.dependencies
    }


def test_failed_dependency_records_no_resource(tmp_path):
    compiled, report = run(
        grammar('<start><externalRef href="missing.rng"/></start>'), path=tmp_path / "root.rng"
    )
    assert "relaxng-resource-read" in codes(report)
    assert len(compiled.dependencies) == 1
    assert isinstance(deref(compiled.start), NotAllowed)


def test_external_grammar_and_inherited_namespace(tmp_path):
    (tmp_path / "external.rng").write_text(
        grammar(f"<start>{element('external')}</start>"), encoding="utf-8"
    )
    compiled, report = run(
        f'<externalRef xmlns="{NS}" ns="urn:adopted" href="external.rng"/>',
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name("urn:adopted", "external")


def test_canonical_recursive_choice_order_is_process_independent():
    xml = grammar(
        '<start><ref name="a"/></start><define name="a"><element name="a">'
        '<choice><ref name="b"/><ref name="a"/></choice></element></define>'
        f'<define name="b">{element("b")}</define>'
    )
    one, report = run(xml)
    two, other_report = run(xml)
    assert not report.errors and not other_report.errors
    assert repr(one.start) == repr(two.start)
    assert [repr(d.pattern) for d in one.definitions.values()] == [
        repr(d.pattern) for d in two.definitions.values()
    ]


@pytest.mark.parametrize(
    "body",
    [
        '<ref name="missing"/>',
        "<ref/>",
        '<parentRef name="missing"/>',
        "<externalRef/>",
    ],
)
def test_references_without_a_scope_or_href(body):
    compiled, report = run(body.replace("/>", f' xmlns="{NS}"/>'))
    assert report.errors
    assert isinstance(deref(compiled.start), NotAllowed)


def test_missing_definition_name_and_missing_ref_name():
    _, report = run(grammar("<start><ref/></start><define><empty/></define>"))
    assert {"relaxng-missing-name", "relaxng-undefined-reference"} <= codes(report)


def test_empty_grammar_and_missing_include_href():
    compiled, report = run(grammar("<include/>"))
    assert {"relaxng-missing-start", "relaxng-missing-href"} <= codes(report)
    assert isinstance(deref(compiled.start), NotAllowed)


@pytest.mark.parametrize(
    "content",
    [
        '<attribute name="a"><ref name="n"/></attribute>',
        '<list><ref name="n"/></list>',
        '<oneOrMore><ref name="n"/></oneOrMore>',
        '<group><ref name="n"/><text/></group>',
        '<interleave><ref name="n"/><text/></interleave>',
    ],
)
def test_not_allowed_reductions_after_reference_expansion(content):
    compiled, report = run(
        grammar(f'<start>{element("root", content)}</start><define name="n"><notAllowed/></define>')
    )
    assert not report.errors
    assert isinstance(deref(compiled.start).pattern, NotAllowed)


@pytest.mark.parametrize(
    "content,expected",
    [
        ('<list><ref name="n"/></list>', "List"),
        ('<oneOrMore><ref name="n"/></oneOrMore>', "OneOrMore"),
        ('<attribute name="a"><ref name="n"/></attribute>', "Attribute"),
        ('<group><ref name="n"/><text/></group>', "Group"),
        ('<interleave><ref name="n"/><text/></interleave>', "Interleave"),
        ('<data type="string"><except><ref name="n"/></except></data>', "DataExcept"),
    ],
)
def test_structural_operands_are_resolved(content, expected):
    compiled, report = run(
        grammar(
            f'<start>{element("root", content)}</start><define name="n"><value>x</value></define>'
        )
    )
    assert not report.errors
    assert type(deref(compiled.start).pattern).__name__ == expected


def test_not_allowed_data_except_becomes_data():
    content = '<data type="string"><except><ref name="n"/></except></data>'
    compiled, report = run(
        grammar(f'<start>{element("root", content)}</start><define name="n"><notAllowed/></define>')
    )
    assert not report.errors
    assert type(deref(compiled.start).pattern).__name__ == "Data"


def test_start_interleave_and_aliases_to_one_element():
    compiled, report = run(
        grammar(
            '<start combine="interleave"><ref name="a"/></start>'
            '<start combine="interleave"><ref name="b"/></start>'
            '<define name="b"><ref name="a"/></define>'
            f'<define name="a">{element("a")}</define>'
        )
    )
    assert not report.errors
    assert isinstance(compiled.start, Interleave)
    assert compiled.start.first is compiled.start.second
    assert len(compiled.definitions) == 1


def test_external_bare_pattern_uses_the_including_grammar(tmp_path):
    (tmp_path / "external.rng").write_text(f'<ref xmlns="{NS}" name="a"/>', encoding="utf-8")
    compiled, report = run(
        grammar(
            '<start><externalRef href="external.rng"/></start>'
            f'<define name="a">{element("a")}</define>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "a")


def test_external_grammar_has_the_including_grammar_as_parent(tmp_path):
    (tmp_path / "external.rng").write_text(
        grammar('<start><parentRef name="a"/></start>'), encoding="utf-8"
    )
    compiled, report = run(
        grammar(
            '<start><externalRef href="external.rng"/></start>'
            f'<define name="a">{element("a")}</define>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "a")


def test_identical_authored_edges_remain_distinct(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<element xmlns="{NS}" name="a"><empty/></element>', encoding="utf-8"
    )
    compiled, report = run(
        grammar(
            '<start><choice><externalRef href="external.rng"/>'
            '<externalRef href="external.rng"/></choice></start>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    dependency = next(d for d in compiled.dependencies if d.references)
    assert len(dependency.references) == 2
    assert dependency.references[0].location != dependency.references[1].location


def test_two_namespace_views_of_one_resource(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<element xmlns="{NS}" name="a"><empty/></element>', encoding="utf-8"
    )
    compiled, report = run(
        grammar(
            '<start><choice><externalRef ns="urn:one" href="external.rng"/>'
            '<externalRef ns="urn:two" href="external.rng"/></choice></start>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert isinstance(compiled.start, Choice)
    assert {
        deref(compiled.start.first).name_class.uri,
        deref(compiled.start.second).name_class.uri,
    } == {"urn:one", "urn:two"}
    assert len(compiled.dependencies) == 2


def test_explicit_external_namespace_wins_over_inheritance(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<element xmlns="{NS}" ns="urn:explicit" name="a"><empty/></element>', encoding="utf-8"
    )
    compiled, report = run(
        grammar('<start ns="urn:parent"><externalRef href="external.rng"/></start>'),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name("urn:explicit", "a")


def test_inherited_namespace_traverses_sibling_indexes(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<element xmlns="{NS}" name="a"><empty/></element>', encoding="utf-8"
    )
    compiled, report = run(
        grammar(
            '<start><choice><element name="local"><empty/></element>'
            '<group ns="urn:ancestor"><empty/>'
            '<externalRef href="external.rng"/></group></choice></start>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert {definition.pattern.name_class.uri for definition in compiled.definitions.values()} == {
        None,
        "urn:ancestor",
    }


def test_undefined_reference_is_checked_before_not_allowed_reduction():
    _, report = run(grammar('<start><group><notAllowed/><ref name="missing"/></group></start>'))
    assert "relaxng-undefined-reference" in codes(report)


def test_external_resources_are_loaded_before_not_allowed_reduction(tmp_path):
    _, report = run(
        grammar('<start><group><notAllowed/><externalRef href="missing.rng"/></group></start>'),
        path=tmp_path / "root.rng",
    )
    assert "relaxng-resource-read" in codes(report)


def test_external_qname_values_use_the_existing_custom_registry_and_read_context(tmp_path):
    libraries = DatatypeLibraryRegistry()
    libraries.register("urn:custom", DatatypeLibraryRegistration(XsdDatatypeFactory(), "custom/1"))
    (tmp_path / "external.rng").write_text(
        f'<value xmlns="{NS}" datatypeLibrary="urn:custom" type="QName">entry</value>',
        encoding="utf-8",
    )
    compiled, report = run(
        grammar('<start ns="urn:adopted"><externalRef href="external.rng"/></start>'),
        path=tmp_path / "root.rng",
        libraries=libraries,
    )
    assert not report.errors
    value = compiled.start
    assert value.datatype.context.namespaces[""] == "urn:adopted"
    assert value.datatype.equal(
        value.lexical,
        value.datatype.context,
        "p:entry",
        DatatypeContext(None, {"p": "urn:adopted"}),
    )


def test_external_unknown_datatype_is_a_capability_failure(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<data xmlns="{NS}" datatypeLibrary="urn:unsupported" type="secret"/>', encoding="utf-8"
    )
    with pytest.raises(UnsupportedDatatypeError, match="unsupported"):
        run(
            grammar('<start><externalRef href="external.rng"/></start>'), path=tmp_path / "root.rng"
        )


def test_composition_uses_the_supplied_pattern_budget():
    loader = ResourceLoader()
    resource = loader.load(
        io.StringIO(
            grammar(f'<start><ref name="a"/></start><define name="a">{element("root")}</define>')
        )
    )
    pool = PatternPool(BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=4)))
    bundle = Simplifier(ValidationReport(), pool, DatatypeLibraryRegistry()).simplify(
        read_full_syntax(resource)
    )
    with pytest.raises(RelaxNGLimitError) as failure:
        compose_grammars(bundle, loader)
    assert failure.value.budget == "max_pattern_nodes"
    assert failure.value.stage == "compile"


def test_namespace_views_do_not_mutate_the_authored_resource():
    resource = ResourceLoader().load(
        io.StringIO(f'<element xmlns="{NS}" name="root"><empty/></element>')
    )
    view = resource.with_namespace("urn:adopted")
    assert view.root is not resource.root
    assert "ns" not in resource.root.attrib
    assert read_full_syntax(view).start.name_class.uri == "urn:adopted"
    assert read_full_syntax(resource).start.name_class.uri is None


def test_closed_unreachable_grammar_still_requires_a_start():
    _, report = run(
        grammar(f'<start>{element("root")}</start><define name="unused"><grammar/></define>')
    )
    assert "relaxng-missing-start" in codes(report)


def test_nested_include_rehomes_nested_parent_ref(tmp_path):
    (tmp_path / "base.rng").write_text(
        grammar(
            '<start><grammar><start><parentRef name="a"/></start></grammar></start>'
            f'<define name="a">{element("old")}</define>'
        ),
        encoding="utf-8",
    )
    compiled, report = run(
        grammar(f'<include href="base.rng"><define name="a">{element("new")}</define></include>'),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "new")


def test_same_names_in_different_nested_scopes_are_distinct():
    compiled, report = run(
        grammar(
            "<start><choice>"
            f'<grammar><start><ref name="x"/></start><define name="x">{element("one")}'
            "</define></grammar>"
            f'<grammar><start><ref name="x"/></start><define name="x">{element("two")}'
            "</define></grammar></choice></start>"
        )
    )
    assert not report.errors
    assert len(compiled.definitions) == 2
    assert {d.pattern.name_class.local for d in compiled.definitions.values()} == {"one", "two"}


@pytest.mark.parametrize(
    "definition",
    [
        '<group><notAllowed/><ref name="bar"/></group>',
        '<group><element name="bar"><empty/></element>'
        '<optional><ref name="bar"/></optional></group>',
    ],
)
def test_recursion_is_checked_before_not_allowed_normalization(definition):
    _, report = run(
        grammar(
            '<start><choice><element name="foo"><empty/></element>'
            '<group><notAllowed/><ref name="bar"/></group></choice></start>'
            f'<define name="bar">{definition}</define>'
        )
    )
    assert "relaxng-recursive-reference" in codes(report)


def test_recursive_choice_identity_is_stable_in_separate_processes():
    xml = grammar(
        '<start><ref name="a"/></start><define name="a"><element name="a">'
        '<choice><ref name="b"/><ref name="a"/></choice></element></define>'
        f'<define name="b">{element("b")}</define>'
    )
    program = """
import sys
from tests.test_relaxng_composition import run
compiled, report = run(sys.argv[1])
assert not report.errors
print(repr(compiled.start))
print([(name, repr(definition.pattern)) for name, definition in compiled.definitions.items()])
"""
    results = [
        subprocess.run(
            [sys.executable, "-c", program, xml],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        for seed in ("1", "42", "random")
    ]
    assert results[0] == results[1] == results[2]


def test_reference_keeps_authored_location_when_groups_collapse(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<element xmlns="{NS}" name="a"><empty/></element>', encoding="utf-8"
    )
    compiled, report = run(
        grammar(
            '<start><group ns="urn:outer"><group ns="urn:inner">'
            '<externalRef href="external.rng"/></group></group></start>'
        ),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name("urn:inner", "a")
    dependency = next(d for d in compiled.dependencies if d.references)
    assert dependency.references[0].location.path[-1].name == f"{{{NS}}}externalRef"


def test_legal_recursion_through_nested_grammar():
    compiled, report = run(
        grammar(
            '<start><ref name="a"/></start><define name="a"><element name="a">'
            '<grammar><start><parentRef name="a"/></start></grammar>'
            "</element></define>"
        )
    )
    assert not report.errors
    root = deref(compiled.start)
    assert deref(root.pattern) is root


def test_recursion_in_a_bare_external_pattern_before_reduction(tmp_path):
    (tmp_path / "external.rng").write_text(
        f'<group xmlns="{NS}"><notAllowed/><ref name="a"/></group>', encoding="utf-8"
    )
    _, report = run(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><externalRef href="external.rng"/></define>'
        ),
        path=tmp_path / "root.rng",
    )
    assert "relaxng-recursive-reference" in codes(report)


def test_nested_grammar_reachability_precedes_not_allowed_reduction():
    _, report = run(
        grammar(
            '<start><group><notAllowed/><grammar><start><parentRef name="a"/></start>'
            "</grammar></group></start>"
            '<define name="a"><ref name="a"/></define>'
        )
    )
    assert "relaxng-recursive-reference" in codes(report)


@pytest.mark.parametrize("present", [False, True])
def test_external_resources_are_read_before_include_overrides(tmp_path, present):
    (tmp_path / "base.rng").write_text(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><externalRef href="external.rng"/></define>'
        ),
        encoding="utf-8",
    )
    if present:
        (tmp_path / "external.rng").write_text(f'<empty xmlns="{NS}"/>', encoding="utf-8")
    compiled, report = run(
        grammar(f'<include href="base.rng"><define name="a">{element("new")}</define></include>'),
        path=tmp_path / "root.rng",
    )
    assert deref(compiled.start).name_class == Name(None, "new")
    if present:
        assert not report.errors
        assert len(compiled.dependencies) == 3
    else:
        assert "relaxng-resource-read" in codes(report)
        assert len(compiled.dependencies) == 2


def test_overridden_nested_scopes_are_removed_before_reference_checks(tmp_path):
    (tmp_path / "base.rng").write_text(
        grammar(
            '<start><ref name="a"/></start>'
            '<define name="a"><grammar><start><ref name="absent"/></start></grammar></define>'
        ),
        encoding="utf-8",
    )
    compiled, report = run(
        grammar(f'<include href="base.rng"><define name="a">{element("new")}</define></include>'),
        path=tmp_path / "root.rng",
    )
    assert not report.errors
    assert deref(compiled.start).name_class == Name(None, "new")


class PermissiveDatatype(Datatype):
    """A datatype that accepts every lexical value."""

    def allows(self, lexical, context):
        return True

    def equal(self, left, left_context, right, right_context):
        return True

    def __repr__(self):
        return "PermissiveDatatype()"


class PermissiveFactory(DatatypeFactory):
    """A custom library that accepts every type name."""

    def create(self, type_name, parameters, context):
        return PermissiveDatatype()


def permissive_library(uri):
    libraries = DatatypeLibraryRegistry()
    libraries.register(uri, DatatypeLibraryRegistration(PermissiveFactory(), "permissive/1"))
    return libraries


@pytest.mark.parametrize(
    "body",
    [
        '<data type="bad:type" datatypeLibrary="urn:permissive"/>',
        '<value type="bad:type" datatypeLibrary="urn:permissive">x</value>',
        '<data type="token" datatypeLibrary="urn:permissive">'
        '<param name="bad:name">v</param></data>',
    ],
)
def test_malformed_type_and_param_names_are_schema_errors(body):
    _, report = run(
        grammar(f"<start>{body}</start>"),
        libraries=permissive_library("urn:permissive"),
    )

    assert "relaxng-invalid-name" in codes(report)


def test_non_ascii_datatype_library_identity_uses_the_xlink_escaped_uri():
    libraries = permissive_library("urn:example:l%C3%A9")
    compiled, report = run(
        grammar('<start><data datatypeLibrary="urn:example:l\u00e9" type="anything"/></start>'),
        libraries=libraries,
    )

    assert not report.errors
    assert isinstance(compiled.start, Data)


def test_nbsp_padded_combine_is_not_normalized():
    _, report = run(grammar('<start combine="\u00a0choice\u00a0"><empty/></start>'))

    assert "relaxng-invalid-combine" in codes(report)


def test_xml_whitespace_padded_combine_is_normalized():
    _, report = run(grammar('<start combine=" \t\r\nchoice "><empty/></start>'))

    assert "relaxng-invalid-combine" not in codes(report)
