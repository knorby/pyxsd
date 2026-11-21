"""Direct full-syntax construction from RELAX NG compact syntax.

These tests pin the core recursive-descent parser of Appendix A.1: the
declaration environment, patterns, name classes, implicit and nested
grammars, references, includes, datatype contexts, virtual RELAX NG paths,
exact compact spans, operator restrictions, and the depth budget high-water
observation. They assert the resulting full-syntax nodes, never serialized
XML.
"""

from __future__ import annotations

import pytest

from pyxsd.locations import IssueLocation, SourceTextSpan
from pyxsd.namespaces import XML_NS, NamespaceContext
from pyxsd.relaxng import compact
from pyxsd.relaxng.compact import read_compact_syntax
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError, RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.origins import OriginTable
from pyxsd.relaxng.resources import LoadedCompactResource
from pyxsd.relaxng.syntax import (
    RELAX_NG_NAMESPACE,
    FullAnyName,
    FullAttribute,
    FullChoice,
    FullData,
    FullDefine,
    FullDiv,
    FullElement,
    FullEmpty,
    FullExcept,
    FullGrammar,
    FullGroup,
    FullInterleave,
    FullList,
    FullMixed,
    FullName,
    FullNameChoice,
    FullNameExcept,
    FullNode,
    FullNsName,
    FullOneOrMore,
    FullOptional,
    FullParam,
    FullSchema,
    FullStart,
    FullText,
    FullValue,
    FullZeroOrMore,
    GrammarScope,
    ReferencePlaceholder,
)

SOURCE_ID = "schema.rnc"
XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"


def make_resource(source: str, *, inherited: str = "") -> LoadedCompactResource:
    return LoadedCompactResource(
        canonical_id=SOURCE_ID,
        content=source.encode("utf-8"),
        namespaces=NamespaceContext(),
        origins=OriginTable(),
        inherited_namespace=inherited,
    )


def parse(
    source: str,
    *,
    inherited: str = "",
    limits: RelaxNGLimits | None = None,
) -> FullSchema:
    return read_compact_syntax(
        make_resource(source, inherited=inherited),
        budget=BudgetCounter("compile", limits),
    )


def origin_location(node: object) -> IssueLocation:
    locations: list[IssueLocation] = []
    for origin in node.origins:  # type: ignore[attr-defined]
        if origin.location is not None:
            locations.append(origin.location)
    assert len(locations) == 1
    return locations[0]


def path_of(node: object) -> tuple[tuple[str, int], ...]:
    return tuple((step.name, step.index) for step in origin_location(node).path)


def span_of(node: object) -> SourceTextSpan:
    span = origin_location(node).span
    assert span is not None
    return span


def grammar_members(schema: FullSchema) -> tuple[FullNode, ...]:
    start = schema.start
    assert isinstance(start, FullGrammar)
    return start.scope.children


def group_children(node: object) -> tuple[FullNode, ...]:
    assert isinstance(node, FullGroup)
    return node.children


# ---------------------------------------------------------------------------
# Core patterns and virtual paths
# ---------------------------------------------------------------------------


def test_element_root_text_constructs_full_nodes_with_exact_paths_and_spans() -> None:
    schema = parse("element root { text }")

    start = schema.start
    assert isinstance(start, FullElement)
    assert start.annotations == ()
    name = start.name_class
    assert isinstance(name, FullName)
    assert (name.lexical, name.local, name.uri) == ("root", "root", None)
    assert len(start.children) == 1
    body = start.children[0]
    assert isinstance(body, FullText)

    assert path_of(start) == (("element", 1),)
    assert path_of(name) == (("element", 1), ("name", 1))
    assert path_of(body) == (("element", 1), ("text", 1))
    assert span_of(start) == SourceTextSpan(0, 7, 1, 1, 1, 8)
    assert span_of(name) == SourceTextSpan(8, 12, 1, 9, 1, 13)
    assert span_of(body) == SourceTextSpan(15, 19, 1, 16, 1, 20)
    assert schema.grammars == ()
    assert schema.findings == ()


def test_top_level_grammar_constructs_an_implicit_scope_with_start_and_define() -> None:
    schema = parse("start = element root { item }\nitem = element item { empty }\n")

    start = schema.start
    assert isinstance(start, FullGrammar)
    scope = start.scope
    assert schema.grammars == (scope,)
    assert scope.parent is None
    assert path_of(start) == (("grammar", 1),)
    assert [type(child) for child in scope.children] == [FullStart, FullDefine]

    authored_start = scope.children[0]
    assert isinstance(authored_start, FullStart)
    assert authored_start.combine is None
    assert path_of(authored_start) == (("grammar", 1), ("start", 1))
    element = authored_start.children[0]
    assert isinstance(element, FullElement)
    assert path_of(element) == (("grammar", 1), ("start", 1), ("element", 1))
    reference = element.children[0]
    assert isinstance(reference, ReferencePlaceholder)
    assert (reference.kind, reference.name, reference.scope) == ("ref", "item", scope)
    assert path_of(reference) == (
        ("grammar", 1),
        ("start", 1),
        ("element", 1),
        ("ref", 1),
    )

    definition = scope.children[1]
    assert isinstance(definition, FullDefine)
    assert (definition.name, definition.combine) == ("item", None)
    assert path_of(definition) == (("grammar", 1), ("define", 1))


def test_combine_assignments_map_to_choice_and_interleave() -> None:
    schema = parse(
        "start = empty\n"
        "start |= text\n"
        "start &= notAllowed\n"
        "item = empty\n"
        "item |= text\n"
        "item &= notAllowed\n"
    )

    starts = [member for member in grammar_members(schema) if isinstance(member, FullStart)]
    defines = [member for member in grammar_members(schema) if isinstance(member, FullDefine)]
    assert [start.combine for start in starts] == [None, "choice", "interleave"]
    assert [define.combine for define in defines] == [None, "choice", "interleave"]


def test_parent_references_keep_the_enclosing_scope() -> None:
    schema = parse("start = element r { parent item }\nitem = empty\n")

    scope = schema.start.scope  # type: ignore[attr-defined]
    component = grammar_members(schema)[0]
    assert isinstance(component, FullStart)
    element = component.children[0]
    assert isinstance(element, FullElement)
    reference = element.children[0]
    assert isinstance(reference, ReferencePlaceholder)
    assert (reference.kind, reference.name, reference.scope) == ("parentRef", "item", scope)
    assert path_of(reference)[-1] == ("parentRef", 1)


def test_nested_grammar_creates_a_child_scope_in_preorder() -> None:
    outer = parse("start = element r { grammar { start = text } }\n")
    outer_scope = outer.start.scope  # type: ignore[attr-defined]
    component = grammar_members(outer)[0]
    assert isinstance(component, FullStart)
    element = component.children[0]
    assert isinstance(element, FullElement)
    inner = element.children[0]
    assert isinstance(inner, FullGrammar)
    assert inner.scope.parent is outer_scope
    assert outer.grammars == (outer_scope, inner.scope)
    assert path_of(inner) == (
        ("grammar", 1),
        ("start", 1),
        ("element", 1),
        ("grammar", 1),
    )
    assert path_of(component) == (("grammar", 1), ("start", 1))
    assert path_of(inner.scope.children[0]) == (
        ("grammar", 1),
        ("start", 1),
        ("element", 1),
        ("grammar", 1),
        ("start", 1),
    )

    bare = parse("element r { grammar { start = text } }")
    assert isinstance(bare.start, FullElement)
    bare_inner = bare.start.children[0]
    assert isinstance(bare_inner, FullGrammar)
    assert bare_inner.scope.parent is None
    assert bare.grammars == (bare_inner.scope,)


def test_include_and_external_keep_href_base_and_inherited_namespace() -> None:
    schema = parse(
        'default namespace x = "urn:x"\n'
        'namespace e = ""\n'
        'include "inc.rnc" { item = element i { empty } }\n'
        'include "empty.rnc" inherit = e\n'
        'start = external "ext.rnc" inherit = x\n'
    )

    scope = schema.start.scope  # type: ignore[attr-defined]
    members = grammar_members(schema)
    first, second, start_component = members
    assert isinstance(first, ReferencePlaceholder)
    assert (first.kind, first.href, first.base) == ("include", "inc.rnc", SOURCE_ID)
    assert first.ns_attribute == "urn:x"
    assert first.scope is scope
    override = first.children[0]
    assert isinstance(override, FullDefine)
    assert override.name == "item"
    assert path_of(first) == (("grammar", 1), ("include", 1))
    assert path_of(override) == (("grammar", 1), ("include", 1), ("define", 1))

    assert isinstance(second, ReferencePlaceholder)
    assert (second.kind, second.href, second.base) == ("include", "empty.rnc", SOURCE_ID)
    assert second.ns_attribute == ""
    assert second.children == ()

    assert isinstance(start_component, FullStart)
    external = start_component.children[0]
    assert isinstance(external, ReferencePlaceholder)
    assert (external.kind, external.href, external.base) == (
        "externalRef",
        "ext.rnc",
        SOURCE_ID,
    )
    assert external.ns_attribute == "urn:x"
    assert external.scope is scope


def test_external_without_inherit_reports_no_namespace_attribute() -> None:
    schema = parse('external "ext.rnc"\n')

    start = schema.start
    assert isinstance(start, ReferencePlaceholder)
    assert (start.kind, start.ns_attribute, start.scope) == ("externalRef", None, None)
    assert path_of(start) == (("externalRef", 1),)
    assert schema.grammars == ()


def test_quantifiers_build_repetition_nodes_and_paths() -> None:
    schema = parse("element r { text? , text* , text+ }")

    group = schema.start.children[0]  # type: ignore[attr-defined]
    assert isinstance(group, FullGroup)
    assert path_of(group) == (("element", 1), ("group", 1))
    assert [type(child) for child in group.children] == [
        FullOptional,
        FullZeroOrMore,
        FullOneOrMore,
    ]
    optional, zero_or_more, one_or_more = group.children
    assert path_of(optional) == (("element", 1), ("group", 1), ("optional", 1))
    assert path_of(zero_or_more) == (("element", 1), ("group", 1), ("zeroOrMore", 1))
    assert path_of(one_or_more) == (("element", 1), ("group", 1), ("oneOrMore", 1))
    assert isinstance(optional.children[0], FullText)  # type: ignore[attr-defined]
    assert span_of(optional) == SourceTextSpan(16, 17, 1, 17, 1, 18)
    assert span_of(group) == SourceTextSpan(18, 19, 1, 19, 1, 20)


def test_list_and_mixed_build_content_containers() -> None:
    schema = parse("element r { list { text } , mixed { text } }")

    group = schema.start.children[0]  # type: ignore[attr-defined]
    list_node, mixed_node = group_children(group)
    assert isinstance(list_node, FullList)
    assert isinstance(list_node.children[0], FullText)
    assert isinstance(mixed_node, FullMixed)
    assert isinstance(mixed_node.children[0], FullText)
    assert path_of(list_node) == (("element", 1), ("group", 1), ("list", 1))
    assert path_of(mixed_node) == (("element", 1), ("group", 1), ("mixed", 1))


def test_data_value_parameters_and_exceptions() -> None:
    schema = parse(
        "element r { "
        "xsd:integer , "
        '(xsd:string { minLength = "1" maxLength = "3" } - (empty | text)) , '
        'string "abc" , '
        '"bare" , '
        "token"
        " }"
    )

    group = schema.start.children[0]  # type: ignore[attr-defined]
    integer, constrained, string_value, bare_value, token = group_children(group)

    assert isinstance(integer, FullData)
    assert (integer.datatype_library, integer.type_name, integer.base_uri) == (
        XSD_DATATYPES,
        "integer",
        SOURCE_ID,
    )
    assert integer.children == ()
    assert integer.namespaces == (("", RELAX_NG_NAMESPACE), ("xml", XML_NS))

    assert isinstance(constrained, FullData)
    assert (constrained.datatype_library, constrained.type_name) == (XSD_DATATYPES, "string")
    assert [type(parameter) for parameter in constrained.parameters] == [
        FullParam,
        FullParam,
    ]
    assert [(parameter.name, parameter.value) for parameter in constrained.parameters] == [
        ("minLength", "1"),
        ("maxLength", "3"),
    ]
    exception = constrained.except_pattern
    assert isinstance(exception, FullExcept)
    excluded = exception.children[0]
    assert isinstance(excluded, FullChoice)
    assert [type(alternative) for alternative in excluded.children] == [FullEmpty, FullText]
    assert path_of(exception) == (*path_of(constrained), ("except", 1))

    assert isinstance(string_value, FullValue)
    assert (
        string_value.datatype_library,
        string_value.type_name,
        string_value.lexical,
    ) == ("", "string", "abc")
    assert string_value.base_uri == SOURCE_ID
    assert string_value.namespace_uri is None

    assert isinstance(bare_value, FullValue)
    assert (bare_value.datatype_library, bare_value.type_name, bare_value.lexical) == (
        "",
        None,
        "bare",
    )

    assert isinstance(token, FullData)
    assert (token.datatype_library, token.type_name) == ("", "token")


def test_every_name_class_form() -> None:
    schema = parse(
        'namespace ex = "urn:ex"\n'
        "element r { "
        "element * { empty } , "
        "element ex:* { empty } , "
        "element (a | b) { empty } , "
        "element * - ex:* { empty } , "
        "element ex:* - (a | b) { empty } , "
        "element ex:foo { empty }"
        " }"
    )

    group = schema.start.children[0]  # type: ignore[attr-defined]
    any_name, any_ns, choice, except_ns, except_choice, cname = group_children(group)

    assert isinstance(any_name, FullElement)
    wildcard = any_name.name_class
    assert isinstance(wildcard, FullAnyName)
    assert wildcard.except_class is None

    assert isinstance(any_ns, FullElement)
    ns_wildcard = any_ns.name_class
    assert isinstance(ns_wildcard, FullNsName)
    assert (ns_wildcard.uri, ns_wildcard.except_class) == ("urn:ex", None)

    assert isinstance(choice, FullElement)
    name_choice = choice.name_class
    assert isinstance(name_choice, FullNameChoice)
    assert [(alternative.lexical, alternative.uri) for alternative in name_choice.alternatives] == [
        ("a", None),
        ("b", None),
    ]
    assert path_of(name_choice) == (
        ("element", 1),
        ("group", 1),
        ("element", 3),
        ("choice", 1),
    )
    assert path_of(name_choice.alternatives[0]) == (
        ("element", 1),
        ("group", 1),
        ("element", 3),
        ("choice", 1),
        ("name", 1),
    )

    assert isinstance(except_ns, FullElement)
    except_wildcard = except_ns.name_class
    assert isinstance(except_wildcard, FullAnyName)
    except_class = except_wildcard.except_class
    assert isinstance(except_class, FullNameExcept)
    assert len(except_class.alternatives) == 1
    assert isinstance(except_class.alternatives[0], FullNsName)
    assert except_class.alternatives[0].uri == "urn:ex"
    assert path_of(except_class) == (
        ("element", 1),
        ("group", 1),
        ("element", 4),
        ("anyName", 1),
        ("except", 1),
    )

    assert isinstance(except_choice, FullElement)
    ns_except_wildcard = except_choice.name_class
    assert isinstance(ns_except_wildcard, FullNsName)
    assert ns_except_wildcard.uri == "urn:ex"
    nested_except = ns_except_wildcard.except_class
    assert isinstance(nested_except, FullNameExcept)
    assert isinstance(nested_except.alternatives[0], FullNameChoice)

    assert isinstance(cname, FullElement)
    qualified = cname.name_class
    assert isinstance(qualified, FullName)
    assert (qualified.lexical, qualified.local, qualified.uri) == ("ex:foo", "foo", "urn:ex")


def test_literal_segments_concatenate() -> None:
    schema = parse('element r { "ab" ~ \'cd\' ~ """e"f""" }')

    value = schema.start.children[0]  # type: ignore[attr-defined]
    assert isinstance(value, FullValue)
    assert value.lexical == 'abcde"f'


# ---------------------------------------------------------------------------
# Namespace and datatype environments
# ---------------------------------------------------------------------------


def test_default_and_attribute_namespaces() -> None:
    schema = parse(
        'default namespace = "urn:e"\n'
        'namespace p = "urn:p"\n'
        "element foo { attribute bar { empty } , attribute p:baz { empty } }"
    )

    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class.uri == "urn:e"  # type: ignore[union-attr]
    first, second = group_children(element.children[0])
    assert isinstance(first, FullAttribute)
    assert first.name_class.uri is None  # type: ignore[union-attr]
    assert isinstance(second, FullAttribute)
    assert second.name_class.uri == "urn:p"  # type: ignore[union-attr]


def test_predeclared_xml_and_xsd_bindings() -> None:
    schema = parse("element r { attribute xml:lang { empty } , xsd:string }")

    group = schema.start.children[0]  # type: ignore[attr-defined]
    attribute, data = group_children(group)
    assert isinstance(attribute, FullAttribute)
    assert attribute.name_class.uri == XML_NS  # type: ignore[union-attr]
    assert isinstance(data, FullData)
    assert (data.datatype_library, data.type_name) == (XSD_DATATYPES, "string")


def test_inherited_namespace_applies_to_unprefixed_names() -> None:
    schema = parse(
        "element root { attribute child { empty } , element item { empty } }",
        inherited="urn:in",
    )

    root = schema.start
    assert isinstance(root, FullElement)
    assert root.name_class.uri == "urn:in"  # type: ignore[union-attr]
    attribute, child = group_children(root.children[0])
    assert isinstance(attribute, FullAttribute)
    assert attribute.name_class.uri is None  # type: ignore[union-attr]
    assert isinstance(child, FullElement)
    assert child.name_class.uri == "urn:in"  # type: ignore[union-attr]

    explicit = parse(
        "default namespace = inherit\nelement root { empty }",
        inherited="urn:inherited",
    )
    explicit_root = explicit.start
    assert isinstance(explicit_root, FullElement)
    assert explicit_root.name_class.uri == "urn:inherited"  # type: ignore[union-attr]


def test_explicit_default_namespace_overrides_the_inherited_value() -> None:
    schema = parse(
        'default namespace = "urn:explicit"\nelement root { empty }',
        inherited="urn:inherited",
    )

    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class.uri == "urn:explicit"  # type: ignore[union-attr]


def test_a_prefix_bound_to_inherit_uses_the_inherited_namespace() -> None:
    schema = parse(
        "namespace p = inherit\nelement root { element p:child { empty } }",
        inherited="urn:inherited",
    )

    root = schema.start
    assert isinstance(root, FullElement)
    child = root.children[0]
    assert isinstance(child, FullElement)
    assert child.name_class.uri == "urn:inherited"  # type: ignore[union-attr]


def test_a_prefix_bound_to_the_empty_namespace_has_no_namespace() -> None:
    schema = parse('namespace e = ""\nelement e:foo { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class.uri is None  # type: ignore[union-attr]


def test_declarations_keep_the_first_binding() -> None:
    schema = parse(
        'namespace p = "urn:first"\n'
        'namespace p = "urn:second"\n'
        "namespace i = inherit\n"
        'namespace e = ""\n'
        'default namespace = "urn:default"\n'
        'default namespace = "urn:other"\n'
        'datatypes d = "urn:d"\n'
        'datatypes d = "urn:other"\n'
        "element root { empty }"
    )
    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class.uri == "urn:default"  # type: ignore[union-attr]
    frame = schema.resource.namespaces.root_bindings
    assert frame["p"] == "urn:first"
    assert frame[""] == RELAX_NG_NAMESPACE
    assert "i" not in frame
    assert "e" not in frame
    assert "d" not in frame


def test_default_namespace_with_a_prefix_binds_both() -> None:
    schema = parse('default namespace ex = "urn:ex"\nelement root { attribute ex:a { empty } }')

    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class.uri == "urn:ex"  # type: ignore[union-attr]
    attribute = element.children[0]
    assert isinstance(attribute, FullAttribute)
    assert attribute.name_class.uri == "urn:ex"  # type: ignore[union-attr]


def test_synthetic_namespace_context_excludes_datatypes_and_unusable_bindings() -> None:
    resource = make_resource(
        'namespace p = "urn:p"\n'
        "namespace i = inherit\n"
        'namespace e = ""\n'
        'datatypes d = "urn:datatype"\n'
        "element root { empty }"
    )
    schema = read_compact_syntax(resource, budget=BudgetCounter("compile"))

    assert schema.resource is not resource
    assert schema.resource.canonical_id == resource.canonical_id
    assert schema.resource.content is resource.content
    assert schema.resource.inherited_namespace == ""
    assert schema.resource.namespaces is not resource.namespaces
    assert schema.resource.namespaces.root_bindings == {
        "": RELAX_NG_NAMESPACE,
        "xml": XML_NS,
        "p": "urn:p",
    }
    assert resource.namespaces.root_bindings == {}
    assert set(schema.resource.origins.origins) >= set(schema.start.origins.origins)


# ---------------------------------------------------------------------------
# Empty bodies and documentation
# ---------------------------------------------------------------------------


def test_empty_and_documentation_only_sources_build_an_empty_grammar() -> None:
    empty = parse("")
    assert isinstance(empty.start, FullGrammar)
    assert empty.start.scope.children == ()
    assert empty.grammars == (empty.start.scope,)

    documented = parse("## a schema without a body\n")
    assert isinstance(documented.start, FullGrammar)
    assert documented.start.scope.children == ()


def test_documentation_tokens_are_accepted_before_patterns_and_components() -> None:
    schema = parse(
        "## outer\n"
        "start = element root {\n"
        "  ## inner\n"
        "  text\n"
        "}\n"
        "## definition\n"
        "item = ## body\n"
        "  empty\n"
    )

    members = grammar_members(schema)
    assert [type(member) for member in members] == [FullStart, FullDefine]
    element = members[0].children[0]  # type: ignore[attr-defined]
    assert isinstance(element, FullElement)
    assert isinstance(element.children[0], FullText)


# ---------------------------------------------------------------------------
# Rejected combinations and exact errors
# ---------------------------------------------------------------------------


def test_pattern_operator_mixing_is_rejected() -> None:
    source = "element r { a, b | c }"
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse(source)
    error = raised.value
    assert error.found == "'|'"
    assert error.expected == ("','",)
    assert "mixed" in str(error) or "parentheses" in str(error)
    assert error.location is not None
    assert error.location.span == SourceTextSpan(17, 18, 1, 18, 1, 19)

    with pytest.raises(RelaxNGCompactSyntaxError) as reverse:
        parse("element r { a | b, c }")
    assert reverse.value.found == "','"


def test_name_class_operator_mixing_is_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as choice_then_except:
        parse("element (a | b - c) { empty }")
    assert choice_then_except.value.found == "'-'"
    assert "parentheses" in str(choice_then_except.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as except_then_choice:
        parse("element (* - b | c) { empty }")
    assert except_then_choice.value.found == "'|'"
    assert "parentheses" in str(except_then_choice.value)


def test_exception_and_postfix_misuse_is_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as not_data:
        parse("element r { text - empty }")
    assert not_data.value.found == "'-'"
    assert "exception" in str(not_data.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as repeated:
        parse("element r { text?* }")
    assert repeated.value.found == "'*'"


@pytest.mark.parametrize("operator", ["|", "&", ","])
def test_data_exception_cannot_mix_with_pattern_operators(operator: str) -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse(f"element r {{ xsd:string - empty {operator} text }}")

    assert raised.value.found == repr(operator)
    assert "mixed" in str(raised.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as appended:
        parse(f"element r {{ text {operator} xsd:string - empty }}")

    assert appended.value.found == repr(operator)
    assert "mixed" in str(appended.value)


def test_parenthesized_data_exception_composes_normally() -> None:
    choice_schema = parse("element r { (xsd:string - empty) | text }")

    choice = choice_schema.start.children[0]  # type: ignore[attr-defined]
    assert isinstance(choice, FullChoice)
    data = choice.children[0]
    assert isinstance(data, FullData)
    assert data.except_pattern is not None
    assert isinstance(choice.children[1], FullText)

    optional_schema = parse("element r { (xsd:string - empty)? }")

    optional = optional_schema.start.children[0]  # type: ignore[attr-defined]
    assert isinstance(optional, FullOptional)
    optional_data = optional.children[0]
    assert isinstance(optional_data, FullData)
    assert optional_data.except_pattern is not None


def test_quoted_identifiers_and_keyword_names() -> None:
    schema = parse(
        'namespace namespace = "urn:n"\n'
        "\\start = element text { attribute element { empty } , \\start }\n"
        'start = element namespace:foo { xsd:string { element = "x" } }\n'
    )

    definition, start_component = grammar_members(schema)
    assert isinstance(definition, FullDefine)
    assert definition.name == "start"
    element = definition.children[0]
    assert isinstance(element, FullElement)
    element_name = element.name_class
    assert isinstance(element_name, FullName)
    assert (element_name.lexical, element_name.local, element_name.uri) == (
        "text",
        "text",
        None,
    )
    attribute, reference = group_children(element.children[0])
    assert isinstance(attribute, FullAttribute)
    attribute_name = attribute.name_class
    assert isinstance(attribute_name, FullName)
    assert (attribute_name.lexical, attribute_name.local) == ("element", "element")
    assert isinstance(reference, ReferencePlaceholder)
    assert (reference.kind, reference.name) == ("ref", "start")

    assert isinstance(start_component, FullStart)
    start_element = start_component.children[0]
    assert isinstance(start_element, FullElement)
    start_name = start_element.name_class
    assert isinstance(start_name, FullName)
    assert (start_name.lexical, start_name.local, start_name.uri) == (
        "namespace:foo",
        "foo",
        "urn:n",
    )
    data = start_element.children[0]
    assert isinstance(data, FullData)
    assert [(parameter.name, parameter.value) for parameter in data.parameters] == [
        ("element", "x")
    ]


def test_nested_parentheses_observe_the_depth_high_water_mark() -> None:
    source = "element r { ((text)) }"

    parse(source, limits=RelaxNGLimits(max_compact_depth=3))
    with pytest.raises(RelaxNGLimitError) as raised:
        parse(source, limits=RelaxNGLimits(max_compact_depth=2))
    assert raised.value.budget == "max_compact_depth"
    assert raised.value.configured == 2
    assert raised.value.observed == 3

    # Two siblings both reach depth two; the observation is a high-water mark,
    # not a cumulative charge.
    parse("element r { (text) , (text) }", limits=RelaxNGLimits(max_compact_depth=2))


def test_unterminated_element_reports_exact_end_of_input_evidence() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse("element r { text")

    error = raised.value
    assert error.found == "end of input"
    assert error.expected == ("'}'",)
    assert error.location is not None
    assert error.location.source == SOURCE_ID
    assert error.location.path == ()
    assert error.location.span == SourceTextSpan(16, 16, 1, 17, 1, 17)
    assert str(error).startswith("schema.rnc:1:17:")


def test_unexpected_symbol_reports_the_expected_pattern() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse("element r { , }")

    error = raised.value
    assert error.found == "','"
    assert "pattern" in str(error)
    assert error.location is not None
    assert error.location.span == SourceTextSpan(12, 13, 1, 13, 1, 14)


def test_invalid_grammar_member_and_keyword_are_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse("element r { grammar { text } }")
    assert raised.value.found == "'text'"

    with pytest.raises(RelaxNGCompactSyntaxError) as keyword:
        parse("element r { start }")
    assert keyword.value.found == "'start'"


def test_unbound_prefixes_are_unrecoverable() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as name:
        parse("element p:foo { empty }")
    assert name.value.found == "'p:foo'"
    assert "prefix" in str(name.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as wildcard:
        parse("element p:* { empty }")
    assert wildcard.value.found == "'p'"

    with pytest.raises(RelaxNGCompactSyntaxError) as datatype:
        parse("element r { p:integer }")
    assert datatype.value.found == "'p:integer'"
    assert "datatype" in str(datatype.value)


def test_pattern_choice_and_interleave_containers() -> None:
    schema = parse("element r { (a | b) , (c & d) }")

    group = schema.start.children[0]  # type: ignore[attr-defined]
    choice, interleave = group_children(group)
    assert isinstance(choice, FullChoice)
    assert isinstance(interleave, FullInterleave)
    assert path_of(choice) == (("element", 1), ("group", 1), ("choice", 1))
    assert path_of(interleave) == (("element", 1), ("group", 1), ("interleave", 1))
    assert span_of(group) == SourceTextSpan(20, 21, 1, 21, 1, 22)
    assert span_of(choice) == SourceTextSpan(15, 16, 1, 16, 1, 17)
    assert span_of(interleave) == SourceTextSpan(25, 26, 1, 26, 1, 27)
    assert path_of(choice.children[0]) == (
        ("element", 1),
        ("group", 1),
        ("choice", 1),
        ("ref", 1),
    )
    assert path_of(choice.children[1]) == (
        ("element", 1),
        ("group", 1),
        ("choice", 1),
        ("ref", 2),
    )


def test_a_leading_definition_selects_top_level_grammar_content() -> None:
    schema = parse("item = empty\n")

    assert isinstance(schema.start, FullGrammar)
    members = grammar_members(schema)
    assert [type(member) for member in members] == [FullDefine]
    assert members[0].name == "item"  # type: ignore[attr-defined]


def test_top_level_div_groups_grammar_components() -> None:
    schema = parse("div { start = text }\n")

    members = grammar_members(schema)
    assert len(members) == 1
    div = members[0]
    assert isinstance(div, FullDiv)
    assert path_of(div) == (("grammar", 1), ("div", 1))
    assert isinstance(div.children[0], FullStart)
    assert path_of(div.children[0]) == (("grammar", 1), ("div", 1), ("start", 1))


def test_include_body_cannot_contain_an_include() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse('include "a.rnc" { include "b.rnc" }')

    assert raised.value.found == "'include'"
    assert "include body" in str(raised.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as nested:
        parse('include "a.rnc" { div { include "b.rnc" } }')

    assert nested.value.found == "'include'"
    assert "include body" in str(nested.value)


def test_name_classes_require_a_name() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as name:
        parse("element (| a) { empty }")
    assert name.value.found == "'|'"
    assert "name class" in str(name.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as exception:
        parse("element (a - b) { empty }")
    assert exception.value.found == "'-'"
    assert "nsName or anyName" in str(exception.value)


def test_exception_cannot_be_repeated() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse("element r { xsd:string - empty * }")

    assert raised.value.found == "'*'"
    assert "exception" in str(raised.value)


def test_trailing_tokens_after_a_pattern_are_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse("element r { empty } text")

    assert raised.value.found == "'text'"
    assert "after the schema body" in str(raised.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as documentation:
        parse("element r { empty }\n## trailing\n")

    assert documentation.value.found == "a documentation comment"
    assert "after the schema body" in str(documentation.value)


def test_missing_assignment_operators_are_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as start:
        parse("start text")
    assert start.value.found == "'text'"
    assert start.value.expected == ("'&='", "'='", "'|='")

    with pytest.raises(RelaxNGCompactSyntaxError) as define:
        parse("element r { grammar { item empty } }")
    assert define.value.found == "'empty'"
    assert define.value.expected == ("'&='", "'='", "'|='")


def test_invalid_declarations_report_exact_evidence() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as missing_equals:
        parse('namespace p "urn:p"\nelement r { empty }')
    assert missing_equals.value.found == "a literal segment"
    assert missing_equals.value.expected == ("'='",)

    with pytest.raises(RelaxNGCompactSyntaxError) as missing_literal:
        parse("namespace p =")
    assert missing_literal.value.found == "end of input"
    assert missing_literal.value.expected == ("a literal segment",)

    with pytest.raises(RelaxNGCompactSyntaxError) as missing_keyword:
        parse('default = "urn:d"')
    assert missing_keyword.value.found == "'='"
    assert missing_keyword.value.expected == ("'namespace'",)

    with pytest.raises(RelaxNGCompactSyntaxError) as missing_prefix:
        parse('namespace = "urn:p"')
    assert missing_prefix.value.found == "'='"
    assert missing_prefix.value.expected == ("an identifier",)

    with pytest.raises(RelaxNGCompactSyntaxError) as missing_concat:
        parse('namespace p = "urn:p" ~')
    assert missing_concat.value.found == "end of input"
    assert missing_concat.value.expected == ("a literal segment",)

    with pytest.raises(RelaxNGCompactSyntaxError) as missing_reference:
        parse("parent = empty")
    assert missing_reference.value.found == "'='"
    assert missing_reference.value.expected == ("an identifier",)

    with pytest.raises(RelaxNGCompactSyntaxError) as unbound_inherit:
        parse('external "x.rnc" inherit = nope')
    assert unbound_inherit.value.found == "'nope'"
    assert "prefix" in str(unbound_inherit.value)


# ---------------------------------------------------------------------------
# Independence
# ---------------------------------------------------------------------------


def test_separate_reads_keep_independent_grammar_scopes() -> None:
    first = parse("start = text\n")
    second = parse("start = text\n")

    first_scope = first.start.scope  # type: ignore[attr-defined]
    second_scope = second.start.scope  # type: ignore[attr-defined]
    assert isinstance(first_scope, GrammarScope)
    assert isinstance(second_scope, GrammarScope)
    assert first_scope is not second_scope
    assert first.grammars == (first_scope,)
    assert second.grammars == (second_scope,)


def test_package_entry_point_is_the_parser_entry_point() -> None:
    assert compact.read_compact_syntax is read_compact_syntax
