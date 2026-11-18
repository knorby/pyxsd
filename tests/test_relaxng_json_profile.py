"""Schema-static RELAX NG profile layouts, independent of observed instances."""

from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.relaxng import RelaxNGSchema, StaleInterpretationError
from pyxsd.relaxng.datatypes import Datatype, DatatypeFactory, DatatypeLibraryRegistration
from pyxsd.relaxng.json_values import scalar_tag

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
INTEGER = '<data type="integer"/>'
A = '<element name="a"><data type="string"/></element>'
B = '<element name="b"><data type="string"/></element>'
ATTR = '<optional><attribute name="z"><data type="integer"/></attribute></optional>'


def schema_for(content, *, name='name="r"'):
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" datatypeLibrary="{XSD}" {name}>{content}</element>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def parse(content, xml="<r/>", *, name='name="r"'):
    document = schema_for(content, name=name).parse(io.StringIO(xml), typed=True)
    assert not document.report.has_errors, document.report.issues
    return document


def export(document, *, plan=None, element=None):
    from pyxsd.relaxng.json_profile import export_profile

    return export_profile(
        document.root, document.schema.projection if plan is None else plan, element=element
    )


def replace_root(document, **changes):
    plan = document.schema.projection
    route_id = plan.roots[0]
    return replace(
        plan, routes={**plan.routes, route_id: replace(plan.routes[route_id], **changes)}
    )


@pytest.mark.parametrize(
    ("content", "xml", "expected"),
    [
        ("<empty/>", "<r/>", ""),
        (ATTR, "<r/>", {}),
        (ATTR, '<r z="3"/>', {"@z": 3}),
        ("<text/>", "<r/>", ""),
        (INTEGER, "<r>12</r>", 12),
        (ATTR + INTEGER, "<r>12</r>", {"#content": 12}),
        (ATTR + INTEGER, '<r z="3">12</r>', {"@z": 3, "#content": 12}),
        (ATTR + "<text/>", "<r/>", {"#content": ""}),
        (f"<interleave>{A}{B}</interleave>", "<r><b>B</b><a>A</a></r>", {"a": "A", "b": "B"}),
        (A + B, "<r><a>A</a><b>B</b></r>", [{"a": "A"}, {"b": "B"}]),
        (f"<choice>{A}{B}</choice>", "<r><a>A</a></r>", [{"a": "A"}]),
        (f"<zeroOrMore>{A}</zeroOrMore>", "<r/>", []),
        (f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>A</a></r>", ["A"]),
        (ATTR + f"<zeroOrMore>{A}</zeroOrMore>", "<r/>", {"a": []}),
        (ATTR + f"<zeroOrMore>{A}</zeroOrMore>", '<r z="3"><a>A</a></r>', {"@z": 3, "a": ["A"]}),
        (f"<zeroOrMore><group>{A}{B}</group></zeroOrMore>", "<r/>", []),
        (
            f"<zeroOrMore><group>{A}{B}</group></zeroOrMore>",
            "<r><a>A</a><b>B</b><a>C</a><b>D</b></r>",
            [{"a": "A"}, {"b": "B"}, {"a": "C"}, {"b": "D"}],
        ),
    ],
)
def test_compiled_layouts(content, xml, expected):
    document = parse(content, xml)
    assert export(document) == {"r": expected}
    assert document.to_data(format="pyxsd-json", fidelity="strict") == {"r": expected}
    assert json.loads(document.to_json(format="pyxsd-json")) == {"r": expected}


def test_root_names_and_explicit_closed_narrowing():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}"><element name="r" ns="urn:a"><empty/></element>'
            '<element name="other"><empty/></element></choice>'
        )
    )
    document = schema.parse(io.StringIO('<r xmlns="urn:a"/>'), typed=True)
    for selector in (None, "r", "{urn:a}r", "Q{urn:a}r"):
        assert export(document, element=selector) == {"Q{urn:a}r": ""}
    for selector in ("other", "Q{urn:wrong}r", "missing"):
        with pytest.raises(PyXSDError, match="root"):
            export(document, element=selector)


def test_local_root_ambiguity_is_schema_static():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}"><choice><name ns="urn:a">r</name>'
            '<name ns="urn:b">r</name></choice><empty/></element>'
        )
    )
    document = schema.parse(io.StringIO('<r xmlns="urn:a"/>'), typed=True)
    assert export(document, element="Q{urn:a}r") == {"Q{urn:a}r": ""}
    with pytest.raises(PyXSDError, match="ambiguous"):
        export(document, element="r")


@pytest.mark.parametrize("name_class", ["<anyName/>", '<nsName ns="urn:a"/>'])
def test_open_start_stays_symbolic_and_accepts_expanded_narrowing(name_class):
    document = parse(name_class + '<data type="string"/>', '<r xmlns="urn:a">x</r>', name="")
    assert export(document) == {"Q{urn:a}r": "x"}
    assert export(document, element="{urn:a}r") == {"Q{urn:a}r": "x"}
    if name_class == "<anyName/>":
        with pytest.raises(PyXSDError, match="ambiguous"):
            export(document, element="r")
    else:
        assert export(document, element="r") == {"Q{urn:a}r": "x"}


def test_root_not_in_plan_is_a_conversion_error():
    document = parse("<empty/>", "<r/>")
    other = parse("<empty/>", "<other/>", name='name="other"')
    with pytest.raises(PyXSDError, match="root"):
        export(document, plan=other.schema.projection)


def test_generic_scalar_and_tags_never_become_child_objects():
    content = '<choice><data type="integer"/><data type="string"/></choice>'
    document = parse(content, "<r>12</r>")
    tag = scalar_tag(document.root.scalar_value())
    assert export(document) == {"r": tag}
    plan = replace_root(document, generic_reason="opaque application projection")
    assert export(document, plan=plan) == {"r": {"#content": tag}}
    assert export(
        parse("<text/>"), plan=replace_root(parse("<text/>"), generic_reason="opaque")
    ) == {"r": {"#content": ""}}


def test_generic_scalar_with_attributes_and_list_interpretation():
    content = ATTR + '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    document = parse(content, '<r z="3">12</r>')
    tag = scalar_tag(document.root.scalar_value())
    attribute_tag = scalar_tag(document.root.attribute_value("z"))
    assert export(document) == {"r": {"@z": attribute_tag, "#content": tag}}
    assert export(document, plan=replace_root(document, generic_reason="opaque")) == {
        "r": {"@z": attribute_tag, "#content": tag}
    }


def test_child_wildcard_uses_generic_scalar_envelope():
    content = '<zeroOrMore><element><anyName/><data type="string"/></element></zeroOrMore>'
    document = parse(content, '<r><x xmlns="urn:c">12</x><y/></r>')
    assert export(document) == {
        "r": [{"Q{urn:c}x": {"#content": "12"}}, {"y": {"#content": ""}}],
    }
    assert export(parse(content)) == {"r": []}


def test_unknown_child_route_is_generic_recursively_not_inferred_from_sample():
    document = parse(f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>12</a></r>")
    plan = replace_root(document, children={})
    assert export(document, plan=plan) == {"r": [{"a": ["12"]}]}
    assert export(document, plan=replace_root(document, generic_reason="opaque")) == {
        "r": [{"a": "12"}]
    }


def test_whole_element_generic_reason_retains_lexical_scalar_and_child_order():
    content = f"<choice>{INTEGER}{A}</choice>"
    text = parse(content, "<r>12</r>")
    child = parse(content, "<r><a>x</a></r>")
    assert text.schema.projection.routes[text.schema.projection.roots[0]].generic_reason
    assert export(text) == {"r": ["12"]}
    assert export(child) == {"r": [{"a": "x"}]}


def test_wildcard_attributes_remain_lexical_and_xsi_names_are_ordinary():
    content = "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>"
    document = parse(
        content,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true" xsi:type="Thing" other="12"/>',
    )
    assert export(document) == {
        "r": {
            "@other": "12",
            "@Q{http://www.w3.org/2001/XMLSchema-instance}nil": "true",
            "@Q{http://www.w3.org/2001/XMLSchema-instance}type": "Thing",
        }
    }


def test_record_attributes_follow_route_order_sequence_attributes_are_sorted():
    attributes = '<attribute name="z"><data type="string"/></attribute><attribute name="a"><data type="string"/></attribute>'
    record = parse(
        attributes + f"<interleave>{A}{B}</interleave>", '<r z="Z" a="A"><b>2</b><a>1</a></r>'
    )
    assert list(export(record)["r"])[:2] == ["@a", "@z"]
    sequence = parse(attributes + A + B, '<r z="Z" a="A"><a>1</a><b>2</b></r>')
    assert export(sequence) == {"r": [{"@a": "A", "@z": "Z"}, {"a": "1"}, {"b": "2"}]}
    assert list(export(sequence)["r"][0]) == ["@a", "@z"]


def test_optional_scalar_attributes_fix_layout_even_when_absent():
    schema = schema_for(ATTR + INTEGER)
    for xml, expected in (
        ("<r>2</r>", {"#content": 2}),
        ('<r z="1">3</r>', {"@z": 1, "#content": 3}),
    ):
        document = schema.parse(io.StringIO(xml), typed=True)
        assert document.to_data(format="pyxsd-json") == {"r": expected}


def test_production_adapter_requires_current_valid_typed_evidence_each_time():
    schema = schema_for(INTEGER)
    document = schema.parse(io.StringIO("<r>12</r>"), typed=True)
    assert document.to_data(format="pyxsd-json") == {"r": 12}
    document.root.set_text("13")
    with pytest.raises(StaleInterpretationError):
        document.to_data(format="pyxsd-json")
    document = document.revalidate()
    assert document.to_data(format="pyxsd-json") == {"r": 13}
    invalid = schema.parse(io.StringIO("<r>bad</r>"), typed=True)
    with pytest.raises(ValidationError):
        invalid.to_data(format="pyxsd-json")
    lexical = schema.parse(io.StringIO("<r>12</r>"), typed=False)
    with pytest.raises(PyXSDError, match="requires a typed document"):
        lexical.to_data(format="pyxsd-json")
    with pytest.raises(TypeError):
        document.to_dict(format="pyxsd-json")


def test_forbidden_child_name_never_exports_a_scalar_marker():
    document = parse(f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>x</a></r>")
    document.root.children[0]._name_ = "#value"
    with pytest.raises(PyXSDError, match="name-invalid"):
        export(document)


def test_mixed_text_and_tails_stay_lexical_while_scalar_slots_are_typed():
    document = parse(
        ATTR + f"<mixed><zeroOrMore>{A}</zeroOrMore></mixed>",
        '<r z="3">before<a>A</a>between<a>B</a>after</r>',
    )
    assert export(document) == {
        "r": [
            {"@z": scalar_tag(document.root.attribute_value("z"))},
            "before",
            {"a": "A"},
            "between",
            {"a": "B"},
            "after",
        ]
    }


@pytest.mark.parametrize(
    ("content", "xml", "changes", "pointer"),
    [
        ("<text/>", "<r>x</r>", {"children": True}, "/r"),
        ("<empty/>", "<r/>", {"text": "unexpected"}, "/r"),
        (A + B, "<r><a>A</a><b>B</b></r>", {"text": "unexpected"}, "/r"),
        (f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>A</a></r>", {"name": "b"}, "/r"),
        (f"<interleave>{A}{B}</interleave>", "<r><a>A</a><b>B</b></r>", {"name": "a"}, "/r/a"),
    ],
)
def test_container_violations_report_json_pointer(content, xml, changes, pointer):
    document = parse(content, xml)
    if "children" in changes:
        document.root.append_child(type(document.root)._new("child", document.root.revision))
    if "text" in changes:
        document.root.set_text(changes["text"])
    if "name" in changes:
        document.root.children[-1]._name_ = changes["name"]
    with pytest.raises(PyXSDError, match="profile-container-invalid") as caught:
        export(document)
    assert f"at {pointer!r}:" in str(caught.value)


def test_open_exclusions_and_name_choices_resolve_locals_without_enumeration():
    document = parse(
        '<choice><nsName ns="urn:a"><except><name>blocked</name></except></nsName>'
        '<name ns="urn:pyxsd:profile:unmentioned">other</name></choice><empty/>',
        '<r xmlns="urn:a"/>',
        name="",
    )
    assert export(document, element="r") == {"Q{urn:a}r": ""}
    with pytest.raises(PyXSDError, match="root-unresolved"):
        export(document, element="blocked")
    with pytest.raises(PyXSDError, match="root-ambiguous"):
        export(document, element="other")
    local = parse("<empty/>")
    assert export(local, element="r") == {"r": ""}
    assert export(local, element="Q{}r") == {"r": ""}


def test_generic_unknown_descendants_preserve_attributes_and_all_text():
    document = parse(
        '<element name="a"><attribute name="n"><text/></attribute>'
        f"<mixed><zeroOrMore>{B}</zeroOrMore></mixed></element>",
        '<r><a n="12">before<b>B</b>after</a></r>',
    )
    plan = replace_root(document, children={})
    assert export(document, plan=plan) == {
        "r": [{"a": [{"@n": "12"}, "before", {"b": ["B"]}, "after"]}]
    }


def test_nonmatching_wildcard_does_not_supply_a_child_route():
    document = parse(
        "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>"
        '<zeroOrMore><element><nsName ns="urn:a"/><data type="string"/></element></zeroOrMore>',
        '<r><x xmlns="urn:a">12</x></r>',
    )
    document.root.children[0]._name_ = "other"
    assert export(document) == {"r": [{"other": ["12"]}]}


class _ResultDatatype(Datatype):
    library_uri = "urn:result"
    type_name = "result"
    parameters = ()
    supports_decode = True

    def __init__(self, result):
        self.result = result

    def allows(self, lexical, context):
        return True

    def equal(self, left, left_context, right, right_context):
        return left == right

    def decode(self, lexical, context):
        return self.result


class _ResultFactory(DatatypeFactory):
    def __init__(self, result):
        self.result = result

    def create(self, type_name, parameters, context):
        return _ResultDatatype(self.result)


@pytest.mark.parametrize("value", [{"not": "a scalar"}, None, [None], (None,)])
@pytest.mark.parametrize("attribute", [False, True])
def test_unrepresentable_decoded_scalars_fail_with_their_json_pointer(value, attribute):
    content = '<data type="result" datatypeLibrary="urn:result"/>'
    if attribute:
        content = f'<attribute name="a">{content}</attribute>'
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r">{content}</element>'),
        datatype_libraries={
            "urn:result": DatatypeLibraryRegistration(_ResultFactory(value), "result/1")
        },
    )
    assert not schema.report.has_errors, schema.report.issues
    document = schema.parse(io.StringIO('<r a="x"/>' if attribute else "<r>x</r>"), typed=True)
    pointer = "/r/@a" if attribute else "/r"
    if isinstance(value, (tuple, list)):
        pointer += "/0"
    with pytest.raises(PyXSDError, match="profile-scalar-invalid") as caught:
        document.to_data(format="pyxsd-json")
    assert f"at {pointer!r}:" in str(caught.value)


def test_declared_attribute_order_is_not_replaced_by_lexical_or_sorted_order():
    document = parse(
        '<attribute name="a"><data type="string"/></attribute>'
        '<attribute name="z"><data type="string"/></attribute>',
        '<r a="A" z="Z"/>',
    )
    plan = document.schema.projection
    route = plan.routes[plan.roots[0]]
    reversed_attributes = dict(reversed(list(route.attributes.items())))
    assert list(
        export(document, plan=replace_root(document, attributes=reversed_attributes))["r"]
    ) == [
        "@z",
        "@a",
    ]


def test_namespace_declarations_alone_are_excluded():
    document = parse(
        "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>", '<r a="x"/>'
    )
    document.root._attribs_["xmlns"] = "urn:default"
    document.root._attribs_["{http://www.w3.org/2000/xmlns/}p"] = "urn:p"
    assert export(document) == {"r": {"@a": "x"}}


@pytest.mark.parametrize("mixed", [False, True])
def test_ordered_attribute_errors_point_into_the_leading_attribute_object(mixed):
    children = A + B
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="r"><attribute name="a">'
            '<data type="result" datatypeLibrary="urn:result"/></attribute>'
            f"{children}</element>"
        ),
        datatype_libraries={
            "urn:result": DatatypeLibraryRegistration(_ResultFactory({"bad": "scalar"}), "result/1")
        },
    )
    assert not schema.report.has_errors, schema.report.issues
    document = schema.parse(
        io.StringIO('<r a="x"><a>A</a><b>B</b></r>'),
        typed=True,
    )
    with pytest.raises(PyXSDError, match="at '/r/0/@a':"):
        export(document, plan=replace_root(document, generic_reason="opaque") if mixed else None)


@pytest.mark.parametrize(
    ("content", "xml", "expected"),
    [
        ('<list><oneOrMore><data type="integer"/></oneOrMore></list>', "<r>1 2</r>", [1, 2]),
        (
            '<zeroOrMore><element name="a"><list><oneOrMore><data type="integer"/>'
            "</oneOrMore></list></element></zeroOrMore>",
            "<r><a>1 2</a></r>",
            [[1, 2]],
        ),
    ],
)
def test_list_scalars_are_not_repeated_children(content, xml, expected):
    assert export(parse(content, xml)) == {"r": expected}


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ("<r/>", {}),
        ("<r><a>A</a></r>", {"a": "A"}),
        ("<r><b>B</b><a>A</a></r>", {"a": "A", "b": "B"}),
    ],
)
def test_max_one_interleave_fields_remain_records_when_optional(xml, expected):
    content = f"<interleave><optional>{A}</optional><optional>{B}</optional></interleave>"
    assert export(parse(content, xml)) == {"r": expected}


def test_interleave_with_repeatable_fields_is_ordered_not_a_record():
    content = f"<interleave><zeroOrMore>{A}</zeroOrMore>{B}</interleave>"
    assert export(parse(content, "<r><a>A</a><b>B</b><a>C</a></r>")) == {
        "r": [{"a": "A"}, {"b": "B"}, {"a": "C"}]
    }


def test_interleave_with_wildcards_is_ordered_not_a_closed_record():
    content = (
        "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>"
        f"<interleave>{A}{B}</interleave>"
    )
    assert export(parse(content, '<r n="1"><b>B</b><a>A</a></r>')) == {
        "r": [{"@n": "1"}, {"b": "B"}, {"a": "A"}]
    }
