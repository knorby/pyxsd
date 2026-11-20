"""Whole-element Clark derivatives, unordered attributes, and weak matches."""

from itertools import permutations
from xml.etree import ElementTree as ET

import pytest

from pyxsd.relaxng import validator as v
from pyxsd.relaxng.datatypes import BuiltinDatatypeFactory, DatatypeContext, XsdDatatypeFactory
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NsName
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    PatternPool,
    Ref,
    Text,
    Value,
    choice,
    nullable,
)
from pyxsd.relaxng.tree import RelaxNGElement

CX = DatatypeContext(None, {})
STRING = BuiltinDatatypeFactory().create("string", (), CX)
TOKEN = BuiltinDatatypeFactory().create("token", (), CX)
INTEGER = XsdDatatypeFactory().create("integer", (), CX)


def element(name, content=None):
    return Element(Name(None, name), Empty() if content is None else content)


def attribute(name, content=None):
    return Attribute(Name(None, name), Text() if content is None else content)


def tree(xml):
    return RelaxNGElement.from_etree(ET.fromstring(xml))


def verdict(xml, pattern):
    return v.validate_element(tree(xml), pattern).valid


def context(**kwargs):
    return v.DerivationContext(CX, PatternPool(), **kwargs)


def test_nullable_after_is_false_without_changing_phase_one_boundary():
    residual = v._After(Empty(), Empty())
    assert v._nullable(residual) is False
    assert v._nullable(Empty()) is True
    assert v.nullable is nullable
    with pytest.raises(TypeError, match="_After"):
        nullable(residual)
    # The private guard is not a second fixed-point implementation; callers
    # must maintain Clark's placement invariant, not hide illegal embeddings.
    with pytest.raises(TypeError, match="_After"):
        v._nullable(Group(residual, Text()))


@pytest.mark.parametrize(
    ("xml", "valid"),
    [("<a/>", True), ("<b/>", True), ("<c/>", False), ("<a><b/></a>", False)],
)
def test_start_choice_and_unexpected_children(xml, valid):
    assert verdict(xml, choice(element("a"), element("b"))) is valid


@pytest.mark.parametrize("depth", [1, 2, 12, 120])
def test_legal_recursive_elements_and_missing_terminal(depth):
    ref = Ref()
    ref.bind(element("n", choice(element("leaf"), ref)))
    assert verdict("<n>" * depth + "<leaf/>" + "</n>" * depth, ref)
    assert not verdict("<n>" * depth + "</n>" * depth, ref)


def test_ordered_children_are_not_unordered_attributes():
    pattern = element("r", Group(element("a"), element("b")))
    assert verdict("<r><a/><b/></r>", pattern)
    assert not verdict("<r><b/><a/></r>", pattern)
    assert not verdict("<r><a/></r>", pattern)


@pytest.mark.parametrize("order", list(permutations(["a", "b", "c"])))
def test_interleave_accepts_every_child_order(order):
    pattern = element("r", Interleave(element("a"), Group(element("b"), element("c"))))
    xml = "<r>" + "".join(f"<{name}/>" for name in order) + "</r>"
    assert verdict(xml, pattern) is (order.index("b") < order.index("c"))


@pytest.mark.parametrize("order", list(permutations(["a", "b", "c"])))
def test_group_attributes_accept_every_order(order):
    pattern = element(
        "r", Group(attribute("a", Data(INTEGER)), Group(attribute("b"), attribute("c")))
    )
    xml = "<r " + " ".join(f'{name}="1"' for name in order) + "/>"
    assert verdict(xml, pattern)


def test_overlapping_attribute_assignments_retain_every_viable_derivative():
    pattern = element(
        "r",
        Group(Attribute(AnyName(), Text()), attribute("a", Value(STRING, "x"))),
    )
    for attrs in ['a="x" b="y"', 'b="y" a="x"']:
        assert verdict(f"<r {attrs}/>", pattern)
    assert not verdict('<r a="wrong" b="y"/>', pattern)
    assert not verdict('<r a="x"/>', pattern)


@pytest.mark.parametrize(
    ("xml", "valid"),
    [
        ("<r/>", False),
        ('<r a="1"/>', True),
        ('<r a="bad"/>', False),
        ('<r a="1" b="2"/>', False),
    ],
)
def test_required_unexpected_and_invalid_attributes(xml, valid):
    assert verdict(xml, element("r", attribute("a", Data(INTEGER)))) is valid


@pytest.mark.parametrize("lexical", ["", " ", "\t\r\n"])
def test_attribute_weak_match_accepts_xml_whitespace_for_nullable_pattern(lexical):
    root = tree("<r/>")
    root.set_attribute("a", lexical)
    assert v.validate_element(root, element("r", attribute("a", Empty()))).valid


def test_attribute_weak_match_does_not_accept_unicode_whitespace_or_skip_presence():
    assert not verdict('<r a="\u00a0"/>', element("r", attribute("a", Empty())))
    assert not verdict("<r/>", element("r", attribute("a", Empty())))
    assert verdict('<r a=" 05 "/>', element("r", attribute("a", Value(INTEGER, "5"))))
    assert verdict('<r a=" a  b "/>', element("r", attribute("a", Value(TOKEN, "a b"))))
    assert not verdict('<r a=" a "/>', element("r", attribute("a", Value(STRING, "a"))))


@pytest.mark.parametrize(
    ("content", "pattern", "valid"),
    [
        ("", Empty(), True),
        (" \t\n", Empty(), True),
        ("x", Empty(), False),
        ("\u00a0", Empty(), False),
        ("", Value(STRING, ""), True),
        (" ", Value(STRING, ""), False),
        ("", List(Empty()), True),
        ("", Data(INTEGER), False),
        (" 1 ", Data(INTEGER), True),
        ("", Text(), True),
    ],
)
def test_empty_and_text_only_weak_children(content, pattern, valid):
    assert verdict(f"<r>{content}</r>", element("r", pattern)) is valid


def test_mixed_content_and_xml_only_whitespace_stripping():
    ordered = Group(element("a"), element("b"))
    mixed = element("r", Interleave(Text(), ordered))
    assert verdict("<r>before<a/>between<b/>after</r>", mixed)
    assert verdict("<r>\n<a/>\t<b/> </r>", element("r", ordered))
    assert not verdict("<r>x<a/><b/></r>", element("r", ordered))
    assert not verdict("<r><a/>x<b/></r>", element("r", ordered))
    assert not verdict("<r><a/><b/>x</r>", element("r", ordered))
    assert not verdict("<r><a/>\u00a0<b/></r>", element("r", ordered))


def test_comments_and_processing_instructions_are_excluded_and_text_is_coalesced():
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    root = RelaxNGElement.from_etree(
        ET.fromstring("<r>a<!--not text-->b<?pi not-text?>c</r>", parser=parser)
    )
    assert v.validate_element(root, element("r", Value(STRING, "abc"))).valid
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    root = RelaxNGElement.from_etree(ET.fromstring("<r><!--x--><?p y?></r>", parser=parser))
    assert v.validate_element(root, element("r")).valid


def test_only_namespace_declarations_are_not_content_attributes():
    assert verdict('<r xmlns:p="urn:p"/>', element("r"))
    assert not verdict('<r xml:lang="en"/>', element("r"))
    root = tree("<r/>")
    root.set_attribute("xmlns", "urn:default")
    root.set_attribute("xmlns:p", "urn:p")
    root.set_attribute("{http://www.w3.org/2000/xmlns/}p", "urn:p")
    assert v.validate_element(root, element("r")).valid


@pytest.mark.parametrize(("name", "value"), [("base", "parts/"), ("space", "preserve")])
def test_xml_context_attributes_are_ordinary_required_or_unexpected_attributes(name, value):
    xml = f'<r xml:{name}="{value}"/>'
    required = element("r", Attribute(Name("http://www.w3.org/XML/1998/namespace", name), Text()))
    assert verdict(xml, required)
    assert not verdict("<r/>", required)
    assert not verdict(xml, element("r"))


@pytest.mark.parametrize("name", ["nil", "type"])
def test_xsi_attributes_have_no_special_meaning(name):
    xml = f'<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:{name}="v"/>'
    assert not verdict(xml, element("r"))
    attr = Attribute(Name("http://www.w3.org/2001/XMLSchema-instance", name), Text())
    assert verdict(xml, element("r", attr))


def test_attribute_and_child_contexts_do_not_leak_between_siblings_or_parent_tail():
    qname = XsdDatatypeFactory().create("QName", (), DatatypeContext(None, {"s": "urn:one"}))
    root = tree('<r a="p:x"><n a="p:x">p:x</n>p:x</r>')
    root.set_namespace_bindings({"p": "urn:one"})
    child = root.children[0]
    child.set_namespace_bindings({"p": "urn:two"})
    first = Value(qname, "s:x")
    second = Data(qname)
    pattern = element(
        "r",
        Group(
            attribute("a", first), Group(element("n", Group(attribute("a", second), second)), first)
        ),
    )
    assert v.validate_element(root, pattern).valid
    child.set_namespace_bindings({})
    assert not v.validate_element(root, pattern).valid


def test_nondeterministic_scalar_branch_is_not_selected_before_later_sibling():
    pattern = element(
        "r",
        Choice(
            Group(element("x", Data(INTEGER)), element("a")),
            Group(element("x", Data(STRING)), element("b")),
        ),
    )
    assert verdict("<r><x>12</x><a/></r>", pattern)
    assert verdict("<r><x>12</x><b/></r>", pattern)
    assert not verdict("<r><x>no</x><a/></r>", pattern)


def test_residuals_retain_accepting_and_nonaccepting_alternatives():
    pattern = Choice(element("r"), Group(element("r"), element("later")))
    outcome = v.validate_element(tree("<r/>"), pattern)
    assert outcome.valid
    assert set(outcome.residuals) == {Empty(), element("later")}
    assert outcome.mismatch is None
    cx = context()
    residual = v.children_derivative(pattern, [tree("<r/>")], cx)
    assert residual == choice(Empty(), element("later"))
    assert v.end_tag_derivative(residual) == NotAllowed()


@pytest.mark.parametrize(
    ("xml", "pattern", "event", "name"),
    [
        ("<wrong/>", element("r"), "start_tag_open", "wrong"),
        ('<r a="x"/>', element("r"), "attribute", "a"),
        ("<r/>", element("r", attribute("a")), "start_tag_close", None),
        ("<r>x</r>", element("r"), "text", None),
        ("<r/>", element("r", element("a")), "end_tag", None),
    ],
)
def test_first_mismatch_preserves_preceding_residual_and_exact_occurrence(
    xml, pattern, event, name
):
    root = tree(xml)
    outcome = v.validate_element(root, pattern)
    assert not outcome.valid
    assert outcome.residuals == ()
    assert outcome.mismatch.event == event
    assert outcome.mismatch.element is root
    assert outcome.mismatch.name == name
    assert outcome.mismatch.preceding != NotAllowed()


def test_mismatch_is_earliest_definitive_failure_across_all_branches():
    root = tree("<r><a/><wrong/><later/></r>")
    outcome = v.validate_element(root, element("r", Group(element("a"), element("b"))))
    assert outcome.mismatch.element is root.children[1]
    assert outcome.mismatch.event == "start_tag_open"
    assert outcome.mismatch.preceding == v._After(element("b"), Empty())


def test_start_open_pushes_continuations_outside_group_and_interleave():
    first = element("a", Text())
    second = element("b")
    assert v.start_tag_open_derivative(Group(first, second), "a") == v._After(Text(), second)
    assert v.start_tag_open_derivative(Interleave(first, second), "b") == v._After(Empty(), first)
    assert v.start_tag_open_derivative(OneOrMore(first), "a") == v._After(
        Text(), choice(Empty(), OneOrMore(first))
    )
    outer = v._After(Group(first, second), element("next"))
    inner = v.start_tag_open_derivative(outer, "a")
    assert inner == v._After(Text(), v._After(second, element("next")))
    assert v.start_tag_close_derivative(inner) == inner
    assert v.end_tag_derivative(inner) == v._After(second, element("next"))


def test_nullable_group_open_keeps_both_element_assignments():
    first = choice(Empty(), element("a"))
    residual = v.start_tag_open_derivative(Group(first, element("a", Text())), "a")
    assert residual == choice(v._After(Empty(), element("a", Text())), v._After(Text(), Empty()))


def test_open_name_class_exclusions_and_namespaces():
    pattern = Element(ExceptName(NsName("urn:n"), Name("urn:n", "no")), Empty())
    assert verdict('<n:yes xmlns:n="urn:n"/>', pattern)
    assert not verdict('<n:no xmlns:n="urn:n"/>', pattern)
    assert not verdict("<yes/>", pattern)


@pytest.mark.parametrize("budget", ["max_derivative_states", "max_memo_entries"])
def test_event_limits_raise_instead_of_false_invalidity(budget):
    with pytest.raises(RelaxNGLimitError) as exc:
        v.validate_element(tree("<r/>"), element("r"), limits=RelaxNGLimits(**{budget: 0}))
    assert exc.value.stage == "validate"
    assert exc.value.budget == budget


def test_events_share_text_counters_and_do_not_intern_into_schema_pool():
    cx = context(limits=RelaxNGLimits(max_derivative_states=1))
    assert v.text_derivative(Text(), "x", cx) == Text()
    with pytest.raises(RelaxNGLimitError):
        v.start_tag_open_derivative(element("r"), "r", cx)
    assert len(cx.pool) == 0
    cx = context()
    pattern = element("r", attribute("a"))
    first = v.start_tag_open_derivative(pattern, "r", cx)
    assert first is v.start_tag_open_derivative(pattern, "r", cx)
    second = v.attribute_derivative(first, "a", "x", cx)
    assert second is v.attribute_derivative(first, "a", "x", cx)
    closed = v.start_tag_close_derivative(second, cx)
    assert closed is v.start_tag_close_derivative(second, cx)
    assert v.end_tag_derivative(closed, cx) is v.end_tag_derivative(closed, cx)
    assert len(cx.pool) == 0


def test_non_element_root_is_structure_mismatch_not_a_budget_error():
    root = RelaxNGElement.from_etree(ET.Comment("not a document element"))
    outcome = v.validate_element(root, element("r"), limits=RelaxNGLimits(max_derivative_states=0))
    assert not outcome.valid
    assert outcome.mismatch.event == "structure"


def test_unfinished_document_retains_required_continuation_for_diagnostics():
    later = element("later")
    outcome = v.validate_element(tree("<r/>"), Group(element("r"), later))
    assert not outcome.valid
    assert outcome.residuals == (later,)
    assert outcome.mismatch.event == "document_end"
    assert outcome.mismatch.preceding is outcome.residual


def test_after_mapping_rejects_illegal_placement_instead_of_losing_a_branch():
    with pytest.raises(TypeError, match="placement invariant"):
        v._apply_after(lambda p: p, Text())


def test_repetition_closes_attributes_but_does_not_close_nested_element_attributes():
    content = OneOrMore(element("n", attribute("a")))
    pattern = element("r", content)
    assert verdict('<r><n a="x"/><n a="y"/></r>', pattern)
    assert not verdict("<r><n/></r>", pattern)
    assert not verdict("<r/>", pattern)
    # Repeated wildcard attributes are legal when the repetition also contains
    # an element; all unmatched attributes must still be rejected at close.
    content = OneOrMore(Group(Attribute(AnyName(), Text()), element("n")))
    assert verdict('<r a="x" b="y"><n/><n/></r>', element("r", content))
    assert not verdict('<r a="x"><n/><n/></r>', element("r", content))


def test_interleaved_attributes_are_unordered_even_with_required_children():
    pattern = element("r", Interleave(Group(attribute("a"), element("n")), attribute("b")))
    assert verdict('<r b="y" a="x"><n/></r>', pattern)
    assert not verdict('<r a="x"><n/></r>', pattern)


def test_recursive_mixed_cycle_terminates_without_skipping_child_constraints():
    ref = Ref()
    ref.bind(OneOrMore(choice(Text(), element("n", ref))))
    pattern = element("r", ref)
    assert verdict("<r>one<n>two<n/>three</n>four</r>", pattern)
    assert not verdict("<r>one<wrong/></r>", pattern)


def test_children_derivative_uses_caller_context_and_restores_it_after_nodes_and_limits():
    qname = XsdDatatypeFactory().create("QName", (), DatatypeContext(None, {"s": "urn:one"}))
    original = DatatypeContext("urn:parent", {"p": "urn:one"})
    cx = context()
    cx.datatype_context = original
    assert v.children_derivative(Value(qname, "s:x"), ["p:", "x"], cx) == Empty()
    child = tree("<n/>")
    child.set_namespace_bindings({"p": "urn:two"})
    assert (
        v.children_derivative(Group(element("n"), Value(qname, "s:x")), [child, "p:x"], cx)
        == Empty()
    )
    assert cx.datatype_context is original
    cx = context(limits=RelaxNGLimits(max_memo_entries=0))
    cx.datatype_context = original
    with pytest.raises(RelaxNGLimitError):
        v.children_derivative(element("n"), [child], cx)
    assert cx.datatype_context is original


def test_attribute_memo_includes_lexical_value_name_and_namespace_context():
    qname = XsdDatatypeFactory().create("QName", (), DatatypeContext(None, {"s": "urn:one"}))
    pattern = Attribute(Name(None, "a"), Value(qname, "s:x"))
    cx = context()
    cx.datatype_context = DatatypeContext(None, {"p": "urn:one"})
    assert v.attribute_derivative(pattern, "a", "p:x", cx) == Empty()
    assert v.attribute_derivative(pattern, "b", "p:x", cx) == NotAllowed()
    assert v.attribute_derivative(pattern, "a", "p:y", cx) == NotAllowed()
    cx.datatype_context = DatatypeContext(None, {"p": "urn:two"})
    assert v.attribute_derivative(pattern, "a", "p:x", cx) == NotAllowed()


def test_unbound_event_reference_is_a_graph_error_not_limit_exhaustion():
    with pytest.raises(ValueError, match="unbound"):
        v.start_tag_open_derivative(Ref(), "r")


def test_invalid_empty_element_name_reports_structure_before_budget_work():
    root = RelaxNGElement.from_etree(ET.Element(""))
    outcome = v.validate_element(root, element("r"), limits=RelaxNGLimits(max_memo_entries=0))
    assert not outcome.valid
    assert outcome.mismatch.event == "structure"


@pytest.mark.parametrize("order", list(permutations(["a", "b", "c"])))
def test_attribute_algebra_is_order_independent_without_sorting_events(order):
    pattern = element("r", Group(attribute("a"), Interleave(attribute("b"), attribute("c"))))
    cx = context()
    residual = v.start_tag_open_derivative(pattern, "r", cx)
    for name in order:
        residual = v.attribute_derivative(residual, name, "x", cx)
    residual = v.start_tag_close_derivative(residual, cx)
    residual = v.children_derivative(residual, [], cx)
    assert v.end_tag_derivative(residual, cx) == Empty()


def test_nested_base_uri_context_reaches_attribute_and_text_datatypes():
    from pyxsd.relaxng.datatypes import Datatype

    class BaseDatatype(Datatype):
        def allows(self, lexical, context):
            return lexical == context.base_uri

        def equal(self, left, left_context, right, right_context):
            return left == right == right_context.base_uri

    datatype = Data(BaseDatatype())
    base = Attribute(Name("http://www.w3.org/XML/1998/namespace", "base"), Text())
    root = tree(
        '<r a="https://example/root/" xml:base="https://example/root/">'
        '<n xml:base="child/" a="https://example/root/child/">'
        "https://example/root/child/</n>https://example/root/</r>"
    )
    pattern = element(
        "r",
        Group(
            Group(base, attribute("a", datatype)),
            Group(
                element("n", Group(base, Group(attribute("a", datatype), datatype))),
                datatype,
            ),
        ),
    )
    assert v.validate_element(root, pattern).valid
    root.children[0].set_base_uri("https://example/wrong/")
    assert not v.validate_element(root, pattern).valid
