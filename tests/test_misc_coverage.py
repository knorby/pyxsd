"""Coverage-rounding tests for the CLI entry, package exports, writers,
content-model compiler helpers, identity-constraint internals and the
remaining element-representative behaviors.

The schema-driven tests pin user-visible contracts (error codes on the
report, misplacement records, bound values); the unit tests exercise pure
helpers with small stubs, mirroring ``tests/test_identity_internals.py``.
"""

import io
import logging
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import ClassVar

import pytest

import pyxsd.parser  # noqa: F401 -- imported first; pyxsd.identity is circular otherwise
from pyxsd.binding import ParseModes
from pyxsd.content_model import (
    ChildMatch,
    Particle,
    _compile_element,
    _compile_group_ref,
    _compile_item,
    _ends_one,
    _head_closure,
    _interleave_open,
    _MatchContext,
    _occurrence,
    _open_attribution_violations,
    _substitution_member_names,
    _trace_one,
    _trace_repeated,
    _wildcard_qname_resolver,
    all_members,
    first_required_name,
    locally_declared_element,
    match_content,
    merge_open_content,
    particle_names,
)
from pyxsd.element_representatives.attribute import Attribute
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    ElementRepresentative,
)
from pyxsd.element_representatives.notation import Notation
from pyxsd.identity import (
    _attributeSelections,
    _collectNodeIdAttributes,
    _contributeIdSpace,
    _elementStepMatches,
    _fieldNodes,
    _isComplexContent,
    _issubclass,
    _legacyParsePath,
    _nameOf,
    _nodeValue,
    _selectNodes,
    _tokenContributions,
    _typedAttributeValue,
    _valueSpaceKey,
    check_identity_constraints,
)
from pyxsd.parser import PyXSD
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.writers import XmlTagWriter
from pyxsd.xpath_subset import parse_xpath_subset
from pyxsd.xsd_data_types import (
    NMTOKENS,
    AnyType,
    Base64Binary,
    Boolean,
    Date,
    Duration,
    HexBinary,
    QName,
)
from pyxsd.xsd_data_types import (
    Decimal as XsdDecimal,
)

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"


@pytest.fixture
def schema_report(tmp_path, monkeypatch):
    """Parse a schema fragment and return the report, instance phase stubbed.

    Mirrors ``tests/test_content_models.py`` so schemas without a root
    element declaration can still be inspected.
    """
    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    schema_path = tmp_path / "schema.xsd"

    def _parse(schema_string):
        schema_path.write_text(XSD_HEAD + schema_string + XSD_TAIL, encoding="utf-8")
        parser = PyXSD(
            io.StringIO("<pyxsd-schema-probe/>"),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        )
        return parser.report

    return _parse


def full_parse(body, xml, tmp_path):
    """Run the complete pipeline (schema build plus instance binding)."""
    schema_path = tmp_path / "schema.xsd"
    instance_path = tmp_path / "instance.xml"
    schema_path.write_text(XSD_HEAD + body + XSD_TAIL, encoding="utf-8")
    instance_path.write_text(xml, encoding="utf-8")
    return PyXSD(
        str(instance_path),
        str(schema_path),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=ParseModes.NAMESPACED,
    )


def schema_tree(body):
    """Build an element-representative tree without a parser attached."""
    root = ET.fromstring(XSD_HEAD + body + XSD_TAIL)
    return ElementRepresentative.factory(root, None)


def find_all(er, class_name, found=None):
    """Every processed descendant (and ``er`` itself) of ``class_name``."""
    if found is None:
        found = []
    if type(er).__name__ == class_name:
        found.append(er)
    for child in getattr(er, "processedChildren", None) or []:
        if child is not None:
            find_all(child, class_name, found)
    return found


def error_codes(report):
    return {issue.code for issue in report.errors}


# ---------------------------------------------------------------------------
# __main__ and package exports
# ---------------------------------------------------------------------------


def test_module_entry_point_runs_the_cli():
    result = subprocess.run(
        [sys.executable, "-m", "pyxsd", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "PyXSD" in result.stdout


def test_module_main_guard_invokes_the_cli(monkeypatch, capsys):
    """``python -m pyxsd`` executes the package's ``__main__`` module."""
    monkeypatch.setattr(sys, "argv", ["pyxsd", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("pyxsd", run_name="__main__")
    assert excinfo.value.code == 0
    assert "PyXSD" in capsys.readouterr().out


def test_importing_the_main_module_does_not_run_the_cli():
    import pyxsd.__main__  # noqa: F401

    # The guard kept the CLI from running during import.


def test_lazy_package_exports_resolve():
    import pyxsd

    assert pyxsd.PyXSD.__name__ == "PyXSD"
    assert pyxsd.XMLNode.__name__ == "XMLNode"
    assert pyxsd.BindingPolicy.__name__ == "BindingPolicy"
    assert pyxsd.ParseModes.NAMESPACED


def test_unknown_package_attribute_raises():
    import pyxsd

    with pytest.raises(AttributeError, match="has no attribute"):
        pyxsd.definitely_not_an_export  # noqa: B018


# ---------------------------------------------------------------------------
# XmlTagWriter
# ---------------------------------------------------------------------------


def test_comment_tag_becomes_comment():
    out = io.StringIO()
    XmlTagWriter("_comment_", {}, "a note", False, False, 0, out)
    assert out.getvalue() == "<!--a note-->"


def test_scalar_value_on_children_bearing_element_is_wrapped():
    out = io.StringIO()
    XmlTagWriter("r", {}, "scalar", True, True, 0, out)
    # No end tag: the tree writer closes elements that have children.
    assert out.getvalue() == "<r>\n   scalar\n"


def test_multi_value_element_without_children_writes_its_end_tag():
    out = io.StringIO()
    XmlTagWriter("r", {}, ["a", "b"], False, True, 0, out)
    assert out.getvalue() == "<r>\n   a\n   b\n</r>\n"


def test_single_scalar_value_is_written_inline():
    out = io.StringIO()
    XmlTagWriter("r", {}, "plain", False, True, 0, out)
    assert out.getvalue() == "<r>plain</r>\n"


def test_write_tabs_honors_an_explicit_tab_count():
    out = io.StringIO()
    writer = XmlTagWriter("r", {}, None, False, False, 1, out)
    writer.writeTabs(4, 2)
    assert out.getvalue() == "    <r/>\n" + " " * 8 + " " * 4


# ---------------------------------------------------------------------------
# Content-model compiler helpers
# ---------------------------------------------------------------------------


def test_occurrence_threshold_treats_99999_as_unbounded():
    assert _occurrence({"maxOccurs": 99999}) == (1, None)
    assert _occurrence({"maxOccurs": 99998}) == (1, 99998)
    assert _occurrence({"minOccurs": "0", "maxOccurs": "unbounded"}) == (0, None)


def test_substitution_member_names_skips_unusable_entries():
    assert _substitution_member_names(None, None) == []

    class _ExplodingSchema:
        def getSchema(self):
            raise RuntimeError("no schema")

    assert _substitution_member_names(_ExplodingSchema(), None) == []

    def _member_type(heads, instance_name, extra_elements=()):
        """A stand-in element representative literally named ``Element``."""

        def get_schema(self):
            return SimpleNamespace(elements=[self, *extra_elements])

        def get_heads(self, py_xsd):
            if heads == "explode":
                raise RuntimeError("no heads")
            return heads

        def _instance_name(self, parser=None):
            if instance_name == "explode":
                raise RuntimeError("no name")
            return instance_name

        cls = type(
            "Element",
            (),
            {
                "name": "h",
                "expandedName": "h",
                "getSchema": get_schema,
                "getSubstitutionGroupHeads": get_heads,
                "instanceName": _instance_name,
            },
        )
        return cls()

    heads_explode = _member_type("explode", "member", extra_elements=(object(),))
    assert _substitution_member_names(heads_explode, None) == []

    name_explode = _member_type(["h"], "explode", extra_elements=(object(),))
    assert _substitution_member_names(name_explode, None) == []

    nameless = _member_type(["h"], None, extra_elements=(object(),))
    assert _substitution_member_names(nameless, None) == []

    named = _member_type(["h"], "member", extra_elements=(object(), "junk"))
    assert _substitution_member_names(named, None) == ["member"]


def test_all_members_sees_through_synthetic_wrappers():
    inner_all = Particle("all", 1, 1, [Particle("element", 1, 1, [], "a")])
    wrapper = Particle("sequence", 1, 1, [inner_all], synthetic=True)
    assert [p.name for p in all_members(wrapper)] == ["a"]

    spliced = Particle("all", 1, 1, [wrapper, Particle("element", 1, 1, [], "b")])
    assert [p.name for p in all_members(spliced)] == ["a", "b"]


def test_interleave_open_rewrites_choice_all_and_leaves_unknown_kinds():
    wildcard = Particle("any", 0, None)
    choice = _interleave_open(
        Particle("choice", 1, 1, [Particle("element", 0, 1, [], "a")]), wildcard
    )
    assert choice.kind == "choice"
    assert choice.children[0].children[0] is wildcard

    all_model = _interleave_open(
        Particle("all", 1, 1, [Particle("element", 0, 1, [], "a")]), wildcard
    )
    assert all_model.kind == "all"
    assert all_model.children[-1] is wildcard

    assert _interleave_open(Particle("bogus"), wildcard).kind == "bogus"


def test_merge_open_content_leaves_unmergeable_models_untouched():
    model = Particle("sequence", 1, 1, [])
    assert merge_open_content(None, SimpleNamespace(mode="suffix", wildcard=None)) is None
    assert merge_open_content(model, None) is model
    assert merge_open_content(model, SimpleNamespace(mode="none", wildcard=None)) is model


def test_compile_item_ignores_unrepresentable_children():
    assert _compile_item(object(), None, frozenset(), None) is None


def test_wildcard_qname_resolver_stays_silent_without_context():
    class _NoSchema:
        def getSchema(self):
            raise AttributeError("detached")

    assert _wildcard_qname_resolver(_NoSchema(), None) is None
    assert (
        _wildcard_qname_resolver(SimpleNamespace(getSchema=lambda: SimpleNamespace()), None) is None
    )


def _element_item(name=None, instance_name="unset"):
    class _Item:
        isElementRef = False

        def getMinOccurs(self):
            return 1

        def getMaxOccurs(self):
            return 1

    _Item.name = name

    if instance_name == "unset":
        return _Item()
    if instance_name is None:
        _Item.instanceName = lambda self, parser=None: None
    elif instance_name is False:
        _Item.instanceName = lambda self: name  # signature without the parser keyword
    else:
        _Item.instanceName = lambda self, parser=None: instance_name
    return _Item()


def test_compile_element_name_fallbacks():
    assert _compile_element(_element_item(), None) is None
    particle = _compile_element(_element_item(name="named", instance_name=None), None)
    assert particle.name == "named"
    particle = _compile_element(_element_item(name="named", instance_name=False), None)
    assert particle.name == "named"


def test_compile_element_reference_falls_back_to_the_local_ref_name():
    class _RefSite:
        isElementRef = True
        referredElement = None
        ref = "pre:foo"
        resolveReference = None

        def getSchema(self):
            return None

        def getMinOccurs(self):
            return 1

        def getMaxOccurs(self):
            return 1

    assert _compile_element(_RefSite(), None).name == "foo"


def _group_ref(owner, ref_site):
    return _compile_group_ref(ref_site, owner, frozenset(), None)


def test_compile_group_ref_rejects_unusable_references():
    assert _group_ref(None, SimpleNamespace(ref="")) is None

    class _NoSchema:
        def getSchema(self):
            return None

    assert _group_ref(_NoSchema(), SimpleNamespace(ref="g")) is None

    empty_schema = SimpleNamespace(getSchema=lambda: SimpleNamespace(groups={}))
    assert _group_ref(empty_schema, SimpleNamespace(ref="g")) is None

    class _NoCompositor:
        expandedName = "g"

        def getCompositor(self):
            return None

    resolver = lambda ref, groups, parser=None: _NoCompositor()  # noqa: E731
    no_compositor_schema = SimpleNamespace(getSchema=lambda: SimpleNamespace(groups={}))
    assert (
        _group_ref(no_compositor_schema, SimpleNamespace(ref="g", resolveReference=resolver))
        is None
    )


def test_compile_group_ref_rejects_a_group_without_a_representable_model():
    class _GroupDefinition:
        pass

    _GroupDefinition.__name__ = "Group"
    definition = _GroupDefinition()
    definition.isRefSite = False

    class _DefHolder:
        expandedName = "g"

        def getCompositor(self):
            return definition

    schema = SimpleNamespace(getSchema=lambda: SimpleNamespace(groups={}))
    resolver = lambda ref, groups, parser=None: _DefHolder()  # noqa: E731
    ref_site = SimpleNamespace(ref="g", resolveReference=resolver)
    assert _group_ref(schema, ref_site) is None


def test_pure_model_queries_on_a_missing_model():
    assert particle_names(None) == set()
    assert locally_declared_element(None, "a") is None
    assert first_required_name(None) is None


def test_head_closure_follows_the_transitive_chain():
    closure = _head_closure({"m1": "h1", "h1": "h2"})
    assert closure["m1"] == frozenset({"h1", "h2"})
    assert closure["h1"] == frozenset({"h2"})


def test_head_closure_ignores_self_heads_and_repeats():
    assert _head_closure({"m": "m"}) == {"m": frozenset()}
    assert _head_closure({"m": "h", "h": "h"}) == {"m": frozenset({"h"}), "h": frozenset()}


def test_substitution_member_is_admitted_by_its_head_particle():
    model = Particle("element", 1, 1, [], "h")
    complete, leftover = match_content(model, [ET.fromstring("<m/>")], member_head_map={"m": "h"})
    assert complete
    assert leftover == []


def test_ends_one_guards_depth_and_unknown_kinds():
    context = _MatchContext({}, lambda node: node.tag, None, False)
    assert _ends_one(Particle("element", 1, 1, [], "a"), [], 0, context, {}, 33) == frozenset()
    assert _ends_one(Particle("weird"), [ET.fromstring("<a/>")], 0, context, {}, 0) == frozenset()


def test_trace_one_skips_infeasible_paths():
    context = _MatchContext({}, lambda node: node.tag, None, False)
    memo = {}

    all_particle = Particle("all", 1, 1, [Particle("element", 1, 1, [], "a")])
    out = []
    _trace_one(all_particle, [ET.fromstring("<a/>")], 0, 5, context, memo, out)
    assert out == []

    choice = Particle("choice", 1, 1, [Particle("element", 1, 1, [], "a")])
    out = []
    _trace_one(choice, [ET.fromstring("<b/>")], 0, 1, context, memo, out)
    assert out == []

    empty_sequence = Particle("sequence", 1, 1, [])
    out = []
    _trace_one(empty_sequence, [], 0, 0, context, memo, out)
    assert out == []

    # An unrepresentable kind falls out of the walk without recording.
    out = []
    _trace_one(Particle("weird"), [ET.fromstring("<a/>")], 0, 1, context, memo, out)
    assert out == []


def test_trace_one_stops_at_an_infeasible_suffix():
    context = _MatchContext({}, lambda node: node.tag, None, False)
    model = Particle(
        "sequence",
        1,
        1,
        [Particle("element", 1, 1, [], "a"), Particle("element", 1, 1, [], "b")],
    )
    out = []
    _trace_one(model, [ET.fromstring("<a/>")], 0, 1, context, {}, out)
    assert out == []


def test_trace_repeated_reports_nothing_for_an_unsatisfiable_particle():
    context = _MatchContext({}, lambda node: node.tag, None, False)
    out = []
    _trace_repeated(Particle("element", 1, 1, [], "a"), [], 0, 0, context, {}, out)
    assert out == []


def test_open_content_attribution_skips_blocked_positions():
    declared = Particle("element", 1, 1, [], "a")
    open_wildcard = Particle("any", 0, None, open_content=True)
    violations = _open_attribution_violations(
        declared,
        [ET.fromstring("<a/>")],
        [ChildMatch(0, open_wildcard)],
        {},
        lambda node: node.tag,
        None,
        False,
        None,
        frozenset({0}),
    )
    assert violations == frozenset()


# ---------------------------------------------------------------------------
# Identity-constraint internals
# ---------------------------------------------------------------------------


def test_issubclass_answers_false_for_non_classes():
    assert _issubclass("not-a-class", int) is False


def test_token_contributions_of_unusable_types():
    assert _tokenContributions(None, "x") is None
    union = type("Union", (), {"_unionMembers": (Boolean,)})
    assert _tokenContributions(union, "not-a-boolean") is None


def test_id_space_contribution_ignores_missing_values():
    report = ValidationReport()
    _contributeIdSpace(type("IDType", (), {}), None, None, {}, [], set(), report)
    assert report.errors == []


def test_name_of_falls_back_to_the_descriptor_name():
    node = SimpleNamespace(_name_=None, _descriptor_=SimpleNamespace(name="declared"))
    assert _nameOf(node) == "declared"


def test_element_step_namespace_wildcard():
    assert _elementStepMatches("{urn:t}*", "{urn:t}a") is True
    assert _elementStepMatches("{urn:t}*", "other") is False


def test_attribute_selection_namespace_wildcard():
    node = SimpleNamespace(_attribs_={"{urn:t}a": "1", "b": "2"}, _wildcardSkipAttributes_=None)
    assert _attributeSelections(node, "{urn:t}*") == [("{urn:t}a", "1")]


def test_complex_content_classification():
    assert _isComplexContent(SchemaBase()) is True
    assert _isComplexContent(AnyType("x")) is True
    assert _isComplexContent(object()) is False


def test_value_space_keys_by_type_family():
    assert _valueSpaceKey(Boolean(1))[0] == "boolean"
    assert _valueSpaceKey(Duration("P1D"))[0] == "duration"
    assert _valueSpaceKey(XsdDecimal("1.0"))[0] == "decimal"
    assert _valueSpaceKey(1.5)[0] == "float"
    assert _valueSpaceKey(Date("2000-01-01"))[0] == "date"
    assert _valueSpaceKey(HexBinary("FF"))[0] == "hexBinary"
    assert _valueSpaceKey(Base64Binary("QQ=="))[0] == "base64Binary"
    assert _valueSpaceKey(QName("a:b"))[0] == "QName"
    assert _valueSpaceKey(object())[0] == ""


def test_value_space_keys_for_list_values():
    assert _valueSpaceKey(["a", "b"]) == ("list", ("a", "b"))
    assert _valueSpaceKey(NMTOKENS("a b"))[0] == "list"
    # A one-item list equals the bare item in the shared string space.
    assert _valueSpaceKey(NMTOKENS("a")) == ("string", "a")


def test_node_value_of_a_non_list_simple_content_is_none():
    assert _nodeValue(SchemaBase()) is None
    assert _nodeValue(object()) is None


class _InstanceNameRaises:
    @classmethod
    def _instance_name_of(cls, descriptor, is_attribute=False):
        raise RuntimeError("no instance name")


class _InstanceNameOther:
    @classmethod
    def _instance_name_of(cls, descriptor, is_attribute=False):
        return "other"


def test_typed_attribute_value_survives_broken_declarations():
    class _RaisingNode(_InstanceNameRaises, SchemaBase):
        def descAttributes(self):
            return {"a": "descriptor"}

    class _OtherNameNode(SchemaBase, _InstanceNameOther):
        def descAttributes(self):
            return {"a": "descriptor"}

    assert _typedAttributeValue(_RaisingNode(), "a") is None
    assert _typedAttributeValue(_OtherNameNode(), "a") is None


def test_root_id_value_binds_nothing_and_is_reported(tmp_path):
    """The validation root's own ID-typed value identifies no element."""
    parser = full_parse('<xs:element name="r" type="xs:ID"/>', "<r>abc</r>", tmp_path)
    unresolved = [issue for issue in parser.report.errors if issue.code == "idref-unresolved"]
    assert len(unresolved) == 1
    assert "identifies no element" in unresolved[0].message


def test_skipped_validation_root_contributes_nothing():
    report = ValidationReport()
    root = SimpleNamespace(_skipped_=True, _children_=[], _attribs_={})
    check_identity_constraints(root, report)
    assert report.errors == []
    assert report.warnings == []


class _StubNode:
    _wildcardSkipAttributes_: ClassVar[tuple] = ()
    _attribs_: ClassVar[dict[str, str]] = {"z": "raw"}

    def __init__(self, instance_name):
        self._instance_name_result = instance_name

    def descAttributes(self):
        return {"a": "descriptor"}

    @classmethod
    def _instance_name_of(cls, descriptor, is_attribute=False):
        return cls._instance_name_result


def test_node_id_space_skips_unusable_descriptors():
    class _Raising(_StubNode):
        @classmethod
        def _instance_name_of(cls, descriptor, is_attribute=False):
            raise RuntimeError("broken")

    class _Nameless(_StubNode):
        _instance_name_result = None

    report = ValidationReport()
    for node in (_Raising(None), _Nameless(None)):
        _collectNodeIdAttributes(node, {}, [], set(), report)
    assert report.errors == []
    assert report.warnings == []


def test_legacy_path_parsing_reports_unsupported_selectors():
    report = ValidationReport()
    constraint = SimpleNamespace(constraintName="c")
    assert _legacyParsePath("a[b]", constraint, report, "selector") is None
    assert _legacyParsePath("parent::a", constraint, report, "selector") is None
    unsupported = [issue for issue in report.warnings if issue.code == "identity-unsupported"]
    assert len(unsupported) == 2


def _tree_with_named_children():
    leaf = SimpleNamespace(_children_=[], _attribs_={}, _name_="a")
    return SimpleNamespace(_children_=[leaf], _attribs_={}, _name_="root"), leaf


def test_field_nodes_walk_descendants_and_deduplicate():
    root, leaf = _tree_with_named_children()
    descendant = parse_xpath_subset(".//a", {}, None, None)
    assert _fieldNodes(root, descendant) == [leaf]

    duplicate_union = parse_xpath_subset("a | a", {}, None, None)
    assert _fieldNodes(root, duplicate_union) == [leaf]


def test_field_nodes_deduplicate_attribute_selections():
    node = SimpleNamespace(
        _children_=[], _attribs_={"x": "1"}, _wildcardSkipAttributes_=None, _name_="a"
    )
    duplicate_attribute_union = parse_xpath_subset("@x | @x", {}, None, None)
    fields = _fieldNodes(node, duplicate_attribute_union)
    assert [field.value for field in fields] == ["1"]


def test_selector_union_results_are_deduplicated():
    root, leaf = _tree_with_named_children()
    report = ValidationReport()
    constraint = SimpleNamespace(constraintName="c", selector=" * | a")
    assert _selectNodes(root, constraint, report) == [leaf]


# ---------------------------------------------------------------------------
# Element representatives: choice / group / simpleContent / notation
# ---------------------------------------------------------------------------


def test_choice_misplacement_inside_a_simple_type(schema_report):
    report = schema_report(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:choice><xs:element name='e'/></xs:choice>"
        "</xs:restriction></xs:simpleType>"
    )
    misplaced = [issue for issue in report.errors if issue.code == "misplaced-declaration"]
    assert len(misplaced) == 1
    assert "choice cannot appear inside SimpleType" in misplaced[0].message


def test_choice_emptiability_reflects_its_children():
    optional = find_all(
        schema_tree(
            "<xs:complexType name='t'><xs:choice>"
            "<xs:element name='a' minOccurs='0'/></xs:choice></xs:complexType>"
        ),
        "Choice",
    )[0]
    assert optional.emptiable is True

    required = find_all(
        schema_tree(
            "<xs:complexType name='t'><xs:choice>"
            "<xs:element name='a'/></xs:choice></xs:complexType>"
        ),
        "Choice",
    )[0]
    assert required.emptiable is False


def test_group_definition_emptiability_follows_its_compositor():
    tree = schema_tree(
        "<xs:group name='g'><xs:sequence>"
        "<xs:element name='z' minOccurs='0'/></xs:sequence></xs:group>"
    )
    group = find_all(tree, "Group")[0]
    assert group.emptiable is True
    assert group.getCompositor() is not None


def test_group_reference_cycle_is_not_emptiable():
    tree = schema_tree(
        "<xs:group name='ga'><xs:sequence><xs:group ref='gb'/></xs:sequence></xs:group>"
        "<xs:group name='gb'><xs:sequence><xs:group ref='ga'/></xs:sequence></xs:group>"
    )
    definitions = {g.name: g for g in find_all(tree, "Group") if not g.isRefSite}
    assert definitions["ga"].emptiable is False


def test_optional_group_reference_is_emptiable_without_resolving():
    tree = schema_tree(
        "<xs:group name='g'><xs:sequence><xs:element name='z'/></xs:sequence></xs:group>"
        "<xs:complexType name='t'><xs:group ref='g' minOccurs='0'/></xs:complexType>"
    )
    ref_site = next(g for g in find_all(tree, "Group") if g.isRefSite)
    assert ref_site.emptiable is True
    assert ref_site.getCompositor() is None


def test_required_reference_to_an_emptiable_group_is_emptiable():
    tree = schema_tree(
        "<xs:group name='g'><xs:sequence>"
        "<xs:element name='z' minOccurs='0'/></xs:sequence></xs:group>"
        "<xs:complexType name='t'><xs:group ref='g'/></xs:complexType>"
    )
    ref_site = next(g for g in find_all(tree, "Group") if g.isRefSite)
    assert ref_site.emptiable is True


def test_group_reference_inside_a_simple_type_is_misplaced(schema_report):
    report = schema_report(
        "<xs:group name='g'><xs:sequence><xs:element name='a'/></xs:sequence></xs:group>"
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:group ref='g'/></xs:restriction></xs:simpleType>"
    )
    misplaced = [issue for issue in report.errors if issue.code == "misplaced-declaration"]
    assert len(misplaced) == 1
    assert "group reference 'g' cannot appear inside SimpleType" in misplaced[0].message


def test_empty_top_level_group_passes_the_occurrence_check(schema_report):
    group = find_all(schema_tree("<xs:group name='g'/>"), "Group")[0]
    group.checkDeclarationLegality()
    assert group.getCompositor() is None

    report = schema_report("<xs:group name='g'/>")
    assert not any("minOccurs" in issue.message for issue in report.errors)


def test_simple_content_reports_a_nested_model_group(schema_report):
    report = schema_report(
        "<xs:complexType name='t'><xs:simpleContent>"
        "<xs:annotation><xs:appinfo>x</xs:appinfo></xs:annotation>"
        "<xs:extension base='xs:string'>"
        "<xs:sequence><xs:element name='a'/></xs:sequence>"
        "</xs:extension></xs:simpleContent></xs:complexType>"
    )
    model_groups = [issue for issue in report.errors if "model group" in issue.message]
    assert len(model_groups) == 1
    assert model_groups[0].code == "declaration-child"
    assert "sequence" in model_groups[0].message


def test_simple_content_ignores_rejected_derivation_children(schema_report):
    report = schema_report(
        "<xs:complexType name='t'><xs:simpleContent>"
        "<xs:extension base='xs:string'>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:extension></xs:simpleContent></xs:complexType>"
    )
    assert not any("model group" in issue.message for issue in report.errors)


class _DetachedParent:
    """A stand-in parent that is not a Schema and owns no components."""

    def findLayerNum(self):
        return 1

    def getSchema(self):
        return None


def _notation_element(**attributes):
    body = "".join(f' {key}="{value}"' for key, value in attributes.items())
    return ET.fromstring(f"<xs:notation xmlns:xs='http://www.w3.org/2001/XMLSchema'{body}/>")


def test_nested_notation_is_misplaced():
    notation = Notation(_notation_element(name="n", public="p"), _DetachedParent())
    assert notation.misplacement[0] == "misplaced-declaration"
    assert "top-level" in notation.misplacement[1]


def test_nameless_notation_skips_the_ncname_check():
    notation = Notation(_notation_element(), _DetachedParent())
    notation.checkDeclarationLegality()
    assert notation.xsdElement.get("name") is None


# ---------------------------------------------------------------------------
# Element representatives: attribute
# ---------------------------------------------------------------------------


class _StubSchema:
    def __init__(self):
        self.components = ComponentTable()

    def getNamespace(self):
        return None


class _TypeWithoutAttributeTable:
    """A containing type that cannot accept attribute declarations."""

    def __init__(self):
        self.schema = _StubSchema()

    def findLayerNum(self):
        return 1

    def getSchema(self):
        return self.schema

    def getContainingType(self):
        return self


def test_attribute_inside_a_type_without_an_attribute_table_is_misplaced():
    element = ET.fromstring(
        "<xs:attribute xmlns:xs='http://www.w3.org/2001/XMLSchema' name='a' type='xs:string'/>"
    )
    attribute = Attribute(element, _TypeWithoutAttributeTable())
    assert attribute.misplacement[0] == "misplaced-declaration"
    assert "attribute 'a'" in attribute.misplacement[1]


ATTRIBUTE_BODY = (
    "<xs:element name='r'><xs:complexType>"
    "<xs:attribute name='a' type='xs:string'/>"
    "</xs:complexType></xs:element>"
)


def _descriptor(tmp_path, body=ATTRIBUTE_BODY, xml="<r/>"):
    parser = full_parse(body, xml, tmp_path)
    return type(parser.schemaRootInstance).a


def test_descriptor_get_returns_the_default_for_an_absent_attribute(tmp_path):
    parser = full_parse(
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute name='a' type='xs:int' default='5'/>"
        "</xs:complexType></xs:element>",
        "<r/>",
        tmp_path,
    )
    assert parser.report.errors == []
    assert parser.schemaRootInstance.a == 5


def test_descriptor_set_reports_values_outside_the_declared_type(tmp_path):
    complex_type_body = (
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute name='a' type='Ct'/>"
        "</xs:complexType></xs:element>"
        "<xs:complexType name='Ct'><xs:sequence>"
        "<xs:element name='x' type='xs:string'/></xs:sequence></xs:complexType>"
    )
    parser = full_parse(complex_type_body, '<r a="boom"/>', tmp_path)
    invalid = [issue for issue in parser.report.errors if issue.code == "invalid-attribute"]
    assert len(invalid) == 1
    assert "cannot be validated" in invalid[0].message


def test_descriptor_set_without_a_parser_logs_invalid_values(tmp_path, monkeypatch, caplog):
    descriptor = _descriptor(
        tmp_path,
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute name='a' type='xs:int'/></xs:complexType></xs:element>",
    )
    monkeypatch.delattr(descriptor, "pyXSD", raising=False)
    with caplog.at_level(logging.ERROR, logger="pyxsd.element_representatives.attribute"):
        Attribute.__set__(descriptor, SimpleNamespace(), "not-an-int")
    assert "has an invalid value" in caplog.text


def test_descriptor_set_without_a_parser_logs_unvalidatable_values(tmp_path, monkeypatch, caplog):
    descriptor = _descriptor(tmp_path)
    monkeypatch.delattr(descriptor, "pyXSD", raising=False)
    monkeypatch.setattr(descriptor, "getType", lambda: type("Plain", (), {}))
    with caplog.at_level(logging.ERROR, logger="pyxsd.element_representatives.attribute"):
        Attribute.__set__(descriptor, SimpleNamespace(), "x")
    assert "cannot be validated" in caplog.text


def test_descriptor_str_names_the_container_and_attribute(tmp_path):
    descriptor = _descriptor(tmp_path)
    assert str(descriptor).endswith("|Attribute|a")


def test_descriptor_get_returns_the_descriptor_default_directly(tmp_path):
    descriptor = _descriptor(
        tmp_path,
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute name='a' type='xs:int' default='5'/>"
        "</xs:complexType></xs:element>",
    )
    # Access through the owning class returns the descriptor itself...
    assert descriptor.owner.a is descriptor
    # ...and an instance without a stored value gets the schema default.
    assert Attribute.__get__(descriptor, SimpleNamespace()) == "5"


def test_descriptor_set_stores_a_correctly_typed_value_without_complaint(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)

    class Plain:
        pass

    monkeypatch.setattr(descriptor, "getType", lambda: Plain)
    holder = SimpleNamespace()
    value = Plain()
    Attribute.__set__(descriptor, holder, value)
    assert holder.a is value


def test_descriptor_delete_removes_the_stored_value(tmp_path):
    descriptor = _descriptor(tmp_path)
    holder = SimpleNamespace()
    holder.__dict__["a"] = 3
    Attribute.__delete__(descriptor, holder)
    assert "a" not in holder.__dict__


def test_reference_site_without_a_referred_declaration_has_no_value_constraints(tmp_path):
    descriptor = _descriptor(tmp_path)
    descriptor.isAttributeRef = True
    descriptor.ref = "ghost"
    assert descriptor.getDefault() is None
    assert descriptor.getFixed() is None


def test_referred_attribute_lookup_needs_a_component_table(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    monkeypatch.setattr(descriptor.getSchema(), "components", None)
    assert Attribute._referredAttribute(descriptor, "anything") is None


def test_referred_attribute_lookup_for_an_undeclared_name(tmp_path):
    descriptor = _descriptor(tmp_path)
    assert Attribute._referredAttribute(descriptor, "NoSuchAttribute") is None


def test_resolved_type_falls_back_to_primitives_without_a_table(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    monkeypatch.setattr(descriptor.getSchema(), "components", None)
    resolved = Attribute._resolvedType(descriptor, "int")
    assert issubclass(resolved, int)


def test_invalid_qname_detection():
    assert Attribute._invalidQName("a b") is True
    assert Attribute._invalidQName("{urn:ok}local") is False
    assert Attribute._invalidQName("xs:int") is False


def test_declared_namespace_without_a_schema_is_none(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    monkeypatch.setattr(descriptor, "getSchema", lambda: None)
    assert descriptor._declaredNamespace() is None


def test_attribute_namespace_check_survives_a_failing_lookup(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)

    def boom():
        raise RuntimeError("no namespace context")

    monkeypatch.setattr(descriptor, "_declaredNamespace", boom)
    descriptor._checkAttributeNamespace()


def test_user_declaration_in_the_xsi_namespace_is_reported(tmp_path):
    schema = (
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='http://www.w3.org/2001/XMLSchema-instance'>"
        "<xs:attribute name='userFoo' type='xs:string'/></xs:schema>"
    )
    (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
    parser = PyXSD(
        io.StringIO("<probe/>"),
        str(tmp_path / "schema.xsd"),
        xmlFileOutput=False,
        transformOutputName=None,
    )
    assert any("XML Schema instance namespace" in issue.message for issue in parser.report.errors)


def test_attribute_declaration_reports_unrecognised_attributes(schema_report):
    report = schema_report("<xs:attribute name='g' type='xs:string' bogus='1'/>")
    assert any("unrecognised attribute 'bogus'" in issue.message for issue in report.errors)


def test_attribute_declaration_reports_an_invalid_form_value(schema_report):
    report = schema_report(
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute name='a' type='xs:string' form='bogus'/>"
        "</xs:complexType></xs:element>"
    )
    assert any("invalid form value 'bogus'" in issue.message for issue in report.errors)


def test_global_attribute_must_not_carry_a_ref(schema_report):
    report = schema_report("<xs:attribute name='g' ref='h'/>")
    assert any("must not carry a ref attribute" in issue.message for issue in report.errors)


def test_attribute_reference_must_not_declare_a_name(schema_report):
    report = schema_report(
        "<xs:attribute name='g' type='xs:string'/>"
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute ref='g' name='x'/></xs:complexType></xs:element>"
    )
    assert any("must not also declare a name" in issue.message for issue in report.errors)


def test_attribute_reference_must_not_declare_a_simple_type(schema_report):
    report = schema_report(
        "<xs:attribute name='g' type='xs:string'/>"
        "<xs:element name='r'><xs:complexType><xs:attribute ref='g'>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:attribute></xs:complexType></xs:element>"
    )
    assert any("must not also declare a simpleType" in issue.message for issue in report.errors)


def test_fixed_override_against_a_missing_declaration_reports_no_mismatch(schema_report):
    report = schema_report(
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute ref='noSuch' fixed='x'/></xs:complexType></xs:element>"
    )
    assert not any("does not match" in issue.message for issue in report.errors)


def test_attribute_reference_fixed_value_must_match_the_declaration(schema_report):
    report = schema_report(
        "<xs:attribute name='g' type='xs:string' fixed='a'/>"
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute ref='g' fixed='b'/></xs:complexType></xs:element>"
    )
    assert any(
        "does not match the referenced declaration's" in issue.message for issue in report.errors
    )


# ---------------------------------------------------------------------------
# Element representatives: extension
# ---------------------------------------------------------------------------


def test_extension_base_resolution_with_a_resolvable_base(tmp_path):
    body = (
        "<xs:complexType name='t'><xs:sequence>"
        "<xs:element name='a'/></xs:sequence></xs:complexType>"
        "<xs:complexType name='u'><xs:complexContent>"
        "<xs:extension base='t'>"
        "<xs:sequence><xs:element name='b'/></xs:sequence>"
        "</xs:extension></xs:complexContent></xs:complexType>"
    )
    parser = full_parse(body, "<r><a>x</a><b>y</b></r>", tmp_path)
    types = [
        entry
        for entries in parser.components.values()
        for entry in entries
        if type(entry).__name__ == "ComplexType"
    ]
    named = next(t for t in types if t.name == "u")
    extension = find_all(named, "Extension")[0]
    assert extension.addBaseToComplexType() is None


def test_extension_base_resolution_warns_for_an_unknown_base(caplog):
    tree = schema_tree(
        "<xs:complexType name='t'><xs:complexContent>"
        "<xs:extension base='noSuchBase'>"
        "<xs:sequence><xs:element name='a'/></xs:sequence>"
        "</xs:extension></xs:complexContent></xs:complexType>"
        "<xs:complexType name='u'><xs:complexContent>"
        "<xs:extension base='t'>"
        "<xs:sequence><xs:element name='b'/></xs:sequence>"
        "</xs:extension></xs:complexContent></xs:complexType>"
    )
    extensions = find_all(tree, "Extension")
    assert [extension.hasNoBase for extension in extensions] == [False, False]
    with caplog.at_level(logging.WARNING, logger="pyxsd.element_representatives.extension"):
        # An unresolvable base is warned about instead of raising...
        assert extensions[0].addBaseToComplexType() is None
        # ...and a resolvable one resolves silently.
        assert extensions[1].addBaseToComplexType() is None
    assert any("could not resolve the base" in message for message in caplog.messages)
