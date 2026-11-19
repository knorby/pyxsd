"""Canonical comparison describes a structural projection, not language inclusion."""

import io
import json

import pytest

from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.comparison import compare_relaxng_structure

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def schema(body, *, wrapper="element"):
    xml = (
        f'<element xmlns="{NS}" name="root">{body}</element>'
        if wrapper == "element"
        else f'<grammar xmlns="{NS}">{body}</grammar>'
    )
    result = RelaxNGSchema.compile(io.StringIO(xml))
    result.require_valid()
    return result


def assert_empty(result):
    assert result["totals"] == {"added": 0, "removed": 0, "changed": 0}
    assert result["changed_details"] == []
    assert result["changed_details_truncated"] is False


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            '<attribute name="a"/><attribute name="b"/><element name="x"><empty/></element>',
            '<element name="x"><empty/></element><attribute name="b"/><attribute name="a"/>',
        ),
        (
            '<choice><element name="a"><empty/></element><element name="b"><text/></element></choice>',
            '<choice><element name="b"><text/></element><element name="a"><empty/></element></choice>',
        ),
        (
            '<interleave><element name="a"><empty/></element><interleave>'
            '<element name="b"><empty/></element><element name="c"><empty/></element>'
            "</interleave></interleave>",
            '<interleave><element name="c"><empty/></element><element name="a"><empty/></element>'
            '<element name="b"><empty/></element></interleave>',
        ),
        (
            '<element name="a:x" xmlns:a="urn:n"><empty/></element>',
            '<element name="b:x" xmlns:b="urn:n"><empty/></element>',
        ),
        (
            f'<value datatypeLibrary="{XSD}" type="QName" xmlns:a="urn:n">a:x</value>',
            f'<value datatypeLibrary="{XSD}" type="QName" xmlns:b="urn:n">b:x</value>',
        ),
        (
            "<element><choice><name>a</name><name>b</name></choice><empty/></element>",
            "<element><choice><name>b</name><name>a</name></choice><empty/></element>",
        ),
        (
            '<group><attribute name="a"/><group><attribute name="b"/>'
            '<attribute name="c"/></group></group>',
            '<attribute name="c"/><attribute name="b"/><attribute name="a"/>',
        ),
        (
            '<optional><attribute name="a"/></optional><attribute name="b"/>',
            '<attribute name="b"/><optional><attribute name="a"/></optional>',
        ),
        (
            "<element><choice><name>a</name><choice><name>b</name><name>c</name>"
            "</choice></choice><empty/></element>",
            "<element><choice><name>c</name><name>a</name><name>b</name></choice><empty/></element>",
        ),
    ],
)
def test_semantically_irrelevant_order_and_prefixes(old, new):
    result = compare_relaxng_structure(schema(old), schema(new))
    assert_empty(result)
    assert "language equivalence" in result["statement"]
    assert "inclusion" in result["statement"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("<empty/>", "<text/>"),
        ('<element name="a"><empty/></element>', '<element name="b"><empty/></element>'),
        (
            '<group><element name="a"><empty/></element><element name="b"><empty/></element></group>',
            '<interleave><element name="a"><empty/></element><element name="b"><empty/></element>'
            "</interleave>",
        ),
        (
            '<element name="a"><empty/></element><element name="b"><empty/></element>',
            '<element name="b"><empty/></element><element name="a"><empty/></element>',
        ),
        (f'<data type="integer" datatypeLibrary="{XSD}"/>', "<text/>"),
        (
            f'<data type="integer" datatypeLibrary="{XSD}"><param name="minInclusive">1</param></data>',
            f'<data type="integer" datatypeLibrary="{XSD}"><param name="minInclusive">2</param></data>',
        ),
        ("<value>a</value>", "<value>b</value>"),
        (
            "<element><anyName><except><name>a</name></except></anyName><empty/></element>",
            "<element><anyName><except><name>b</name></except></anyName><empty/></element>",
        ),
        (
            f'<value datatypeLibrary="{XSD}" type="QName" xmlns:a="urn:one">a:x</value>',
            f'<value datatypeLibrary="{XSD}" type="QName" xmlns:a="urn:two">a:x</value>',
        ),
        (
            '<choice><element name="a"><value>x</value></element>'
            '<element name="a"><value>y</value></element></choice>',
            '<element name="a"><text/></element>',
        ),
        (
            '<data type="token"><except><value>x</value></except></data>',
            '<data type="token"><except><value>y</value></except></data>',
        ),
        (
            '<element><nsName ns="urn:one"/><text/></element>',
            '<element><nsName ns="urn:two"/><text/></element>',
        ),
        (
            "<list><value>x</value><value>y</value></list>",
            "<list><value>y</value><value>x</value></list>",
        ),
        (
            '<optional><element name="a"><empty/></element></optional>',
            '<oneOrMore><element name="a"><empty/></element></oneOrMore>',
        ),
    ],
)
def test_semantic_differences_survive_normalization(old, new):
    before, after = schema(old), schema(new)
    result = compare_relaxng_structure(before, after)
    assert result["totals"]["added"]
    assert result["totals"]["removed"]
    assert "start" in result["changed"]
    assert any(item.startswith("projection:") for item in result["changed"])
    assert result == compare_relaxng_structure(before, after)
    reverse = compare_relaxng_structure(after, before)
    assert result["added"] == reverse["removed"]
    assert result["removed"] == reverse["added"]
    assert result["changed"] == reverse["changed"]


def test_categories_details_limits_and_no_provenance(tmp_path):
    old = schema(f'<data type="integer" datatypeLibrary="{XSD}"/>')
    new = schema('<element name="extra"><list><data type="token"/></list></element>')
    result = compare_relaxng_structure(old, new)
    assert set(result["categories"]) == {
        "starts",
        "definitions",
        "patterns",
        "name_classes",
        "datatypes",
        "dependencies",
        "projections",
    }
    for kind in ("definitions", "patterns", "name_classes", "datatypes", "projections"):
        assert result["categories"][kind]["added"] or result["categories"][kind]["changed"]
    assert any(
        change["field"] == "pattern"
        for entry in result["changed_details"]
        for change in entry["changes"]
    )
    limited = compare_relaxng_structure(old, new, detail_limit=0)
    assert limited["totals"] == result["totals"]
    assert limited["changed_details"] == []
    assert limited["changed_details_truncated"] is True
    assert len(compare_relaxng_structure(old, new, detail_limit=1)["changed_details"]) == 1
    with pytest.raises(ValueError, match="non-negative"):
        compare_relaxng_structure(old, new, detail_limit=-1)
    serialized = json.dumps(result)
    assert str(tmp_path) not in serialized
    assert "source_id" not in serialized and "location" not in serialized
    assert_empty(compare_relaxng_structure(old, old))


def test_recursive_definition_renumbering_and_changes():
    body = (
        '<start><ref name="node"/></start><define name="node"><element name="node">'
        '<choice><empty/><group><attribute name="id"/><ref name="node"/></group></choice>'
        "</element></define>"
    )
    before = schema(body, wrapper="grammar")
    equal = schema(
        body.replace('name="node"', 'name="renamed"').replace(
            '<element name="renamed">', '<element name="node">'
        ),
        wrapper="grammar",
    )
    assert_empty(compare_relaxng_structure(before, equal))
    after = schema(body.replace('name="id"', 'name="key"'), wrapper="grammar")
    assert compare_relaxng_structure(before, after)["totals"]["changed"] > 0


def test_relocated_dependencies_compare_resolved_structure_not_bytes(tmp_path):
    schemas = []
    for directory, filename, prefix in (("old", "one.rng", "a"), ("new", "two.rng", "b")):
        root = tmp_path / directory
        root.mkdir()
        (root / filename).write_text(
            f'<element xmlns="{NS}" xmlns:{prefix}="urn:n" name="{prefix}:leaf"><empty/></element>'
        )
        main = root / "main.rng"
        main.write_text(f'<externalRef xmlns="{NS}" href="{filename}"/>')
        compiled = RelaxNGSchema.compile(main)
        compiled.require_valid()
        schemas.append(compiled)
    result = compare_relaxng_structure(*schemas)
    assert_empty(result)
    assert str(tmp_path) not in json.dumps(result)


def test_invalid_schema_is_rejected():
    bad = RelaxNGSchema.compile(io.StringIO(f'<ref xmlns="{NS}" name="missing"/>'))
    from pyxsd.exceptions import ValidationError

    with pytest.raises(ValidationError):
        compare_relaxng_structure(bad, schema("<empty/>"))


def test_dependency_addition_and_removal_are_reported(tmp_path):
    direct = tmp_path / "direct.rng"
    indirect = tmp_path / "indirect.rng"
    direct.write_text(f'<element xmlns="{NS}" name="root"><empty/></element>')
    indirect.write_text(f'<externalRef xmlns="{NS}" href="direct.rng"/>')
    old, new = RelaxNGSchema.compile(direct), RelaxNGSchema.compile(indirect)
    result = compare_relaxng_structure(old, new)
    assert result["categories"]["dependencies"]["added"]
    assert result["categories"]["patterns"] == {"added": [], "removed": [], "changed": []}
    assert compare_relaxng_structure(new, old)["categories"]["dependencies"]["removed"]


def test_shared_recursive_interiors_have_finite_comparisons():
    from tests.test_relaxng_values import shared_interior_schema

    before = RelaxNGSchema.compile(io.StringIO(shared_interior_schema(multi_anchor=True)))
    after = RelaxNGSchema.compile(
        io.StringIO(shared_interior_schema(multi_anchor=True, reverse=True))
    )
    assert_empty(compare_relaxng_structure(before, after))


def test_nested_name_choices_in_checked_graphs_normalize_like_flat_choices():
    from dataclasses import replace

    from pyxsd.relaxng.names import Name, NameChoice
    from pyxsd.relaxng.patterns import Element, Empty, PatternPool
    from pyxsd.relaxng.simplify import CompiledGrammar

    names = tuple(Name(None, name) for name in ("a", "b", "c"))
    base = schema("<empty/>")
    nested = Element(NameChoice((names[0], NameChoice(names[1:]))), Empty())
    flat = Element(NameChoice(tuple(reversed(names))), Empty())
    before = replace(base, grammar=CompiledGrammar(nested, {}, PatternPool(), ()))
    after = replace(base, grammar=CompiledGrammar(flat, {}, PatternPool(), ()))
    assert_empty(compare_relaxng_structure(before, after))


def test_registry_identity_is_part_of_the_datatype_contract():
    from dataclasses import replace

    before = schema("<text/>")
    after = replace(before, datatype_fingerprint=before.datatype_fingerprint + "-changed")
    result = compare_relaxng_structure(before, after)
    assert result["categories"]["datatypes"]["changed"] == ["datatype-registry"]


def test_projection_keeps_same_name_assignment_ordering_correlated():
    before = schema(
        '<element name="a"><value>x</value></element><element name="a"><value>y</value></element>'
    )
    after = schema(
        '<element name="a"><value>y</value></element><element name="a"><value>x</value></element>'
    )
    result = compare_relaxng_structure(before, after)
    assert result["categories"]["projections"]["changed"]


def test_changed_datatype_details_include_parameter_values():
    old = schema(
        f'<data type="integer" datatypeLibrary="{XSD}"><param name="minInclusive">1</param></data>'
    )
    new = schema(
        f'<data type="integer" datatypeLibrary="{XSD}"><param name="minInclusive">2</param></data>'
    )
    details = json.dumps(
        compare_relaxng_structure(old, new)["changed_details"], separators=(",", ":")
    )
    assert '["minInclusive","1"]' in details
    assert '["minInclusive","2"]' in details


def qname_value(prefix):
    return f'<value datatypeLibrary="{XSD}" type="QName" xmlns:{prefix}="urn:n">{prefix}:x</value>'


def test_choice_inventory_ignores_duplicate_canonical_prefix_alternatives():
    old = f"<choice>{qname_value('p')}{qname_value('q')}<value>other</value></choice>"
    new = old.replace("xmlns:p=", "xmlns:q=").replace(">p:x<", ">q:x<")
    assert_empty(compare_relaxng_structure(schema(old), schema(new)))


def test_choice_pattern_inventory_deduplicates_canonical_alternatives():
    from pyxsd.relaxng.comparison import _inventory

    body = f"<choice>{qname_value('p')}{qname_value('q')}<value>other</value></choice>"
    descriptions = [
        entry["unordered"]
        for entry in _inventory(schema(body)).values()
        if entry["category"] == "patterns" and entry.get("kind") == "Choice"
    ]
    assert descriptions
    for unordered in descriptions:
        assert len(unordered) == len(set(unordered))
        assert len(unordered) == 2


def test_definitions_and_projection_ignore_equivalent_assignment_pool_sharing():
    old = (
        f'<element name="a">{qname_value("p")}</element>'
        f'<element name="a">{qname_value("q")}</element>'
    )
    new = old.replace("xmlns:p=", "xmlns:q=").replace(">p:x<", ">q:x<")
    assert_empty(compare_relaxng_structure(schema(old), schema(new)))


def test_equivalent_ordered_child_occurrences_are_not_collapsed():
    once = f'<element name="a">{qname_value("q")}</element>'
    twice = f'<element name="a">{qname_value("p")}</element>{once}'
    result = compare_relaxng_structure(schema(once), schema(twice))
    assert "start" in result["changed"]
    assert result["categories"]["projections"]["changed"]
    assert (
        schema(once)
        .parse(io.StringIO("<root><a xmlns:q='urn:n'>q:x</a></root>"))
        .report.error_count
        == 0
    )
    assert (
        schema(twice)
        .parse(io.StringIO("<root><a xmlns:q='urn:n'>q:x</a></root>"))
        .report.error_count
        > 0
    )


def test_interleave_graph_retains_repeated_operands():
    from pyxsd.relaxng.comparison import _Graph

    # The comparison graph must retain multiplicity even though checked RNG
    # restricts overlapping element/attribute names in authored interleaves.
    graph = _Graph()
    graph.nodes = {
        0: ({"kind": "Text"}, [], []),
        1: ({"kind": "Empty"}, [], []),
        2: ({"kind": "Interleave"}, [], [0, 1]),
        3: ({"kind": "Interleave"}, [], [0, 0, 1]),
    }
    assert graph.identity(2) != graph.identity(3)
