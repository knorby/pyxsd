"""Annotations and recoverable constraints in the RELAX NG compact syntax.

These tests pin Appendix A.1 annotation productions: documentation
shorthand, initial bracket annotations with attributes and nested foreign
content, following ``>>`` annotations and their placement as foreign
siblings, grammar and include-content annotations, quoted keyword names, and
recoverable named constraints with exact spans.  They assert the resulting
full-syntax nodes, opaque ElementTree fragments, and origin evidence, never
serialized XML.
"""

from __future__ import annotations

import pytest

from pyxsd.locations import IssueLocation, PathStep, SourceTextSpan
from pyxsd.namespaces import XML_NS, NamespaceContext
from pyxsd.relaxng.compact import read_compact_syntax
from pyxsd.relaxng.errors import RelaxNGCompactSyntaxError, RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.origins import OriginTable
from pyxsd.relaxng.resources import LoadedCompactResource
from pyxsd.relaxng.syntax import (
    RELAX_NG_NAMESPACE,
    Annotation,
    FullAnyName,
    FullData,
    FullDefine,
    FullElement,
    FullEmpty,
    FullGrammar,
    FullGroup,
    FullName,
    FullNameChoice,
    FullNameExcept,
    FullNode,
    FullOptional,
    FullParam,
    FullSchema,
    FullStart,
    FullText,
    FullZeroOrMore,
    ReferencePlaceholder,
)

SOURCE_ID = "schema.rnc"
XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"
COMPAT_ANNOTATIONS = "http://relaxng.org/ns/compatibility/annotations/1.0"
XMLNS_NAMESPACE = "http://www.w3.org/2000/xmlns"


def make_resource(source: str) -> LoadedCompactResource:
    return LoadedCompactResource(
        canonical_id=SOURCE_ID,
        content=source.encode("utf-8"),
        namespaces=NamespaceContext(),
        origins=OriginTable(),
        inherited_namespace="",
    )


def parse(source: str, *, limits: RelaxNGLimits | None = None) -> FullSchema:
    return read_compact_syntax(
        make_resource(source),
        budget=BudgetCounter("compile", limits),
    )


def origin_location(node: object) -> IssueLocation:
    locations: list[IssueLocation] = []
    for origin in node.origins:  # type: ignore[attr-defined]
        if origin.location is not None:
            locations.append(origin.location)
    assert len(locations) == 1
    return locations[0]


def annotation_location(annotation: Annotation) -> IssueLocation:
    locations = [
        origin.location
        for origin in annotation.origins
        if origin.location is not None and origin.location.attribute is None
    ]
    assert len(locations) == 1
    return locations[0]


def annotation_path(annotation: Annotation) -> tuple[tuple[str, int], ...]:
    return tuple((step.name, step.index) for step in annotation_location(annotation).path)


def span_for(annotation: Annotation) -> SourceTextSpan:
    span = annotation_location(annotation).span
    assert span is not None
    return span


def expected_span(source: str, text: str, occurrence: int = 0) -> SourceTextSpan:
    start = -1
    for _ in range(occurrence + 1):
        start = source.index(text, start + 1)
    return expected_span_at(source, start, start + len(text))


def expected_span_at(source: str, start: int, end: int) -> SourceTextSpan:
    line = source.count("\n", 0, start) + 1
    column = start - source.rfind("\n", 0, start)
    end_line = source.count("\n", 0, end) + 1
    end_column = end - source.rfind("\n", 0, end)
    return SourceTextSpan(start, end, line, column, end_line, end_column)


def annotations_of(node: object) -> tuple[Annotation, ...]:
    return node.annotations  # type: ignore[attr-defined]


def elements_of(node: object) -> list[Annotation]:
    return [annotation for annotation in annotations_of(node) if annotation.kind == "element"]


def attributes_of(node: object) -> dict[str, Annotation]:
    return {
        annotation.name: annotation
        for annotation in annotations_of(node)
        if annotation.kind == "attribute"
    }


def element_annotation(node: object, name: str) -> Annotation:
    matches = [
        annotation
        for annotation in elements_of(node)
        if annotation.name == name and annotation.namespace == "urn:a"
    ]
    assert len(matches) == 1
    return matches[0]


def codes(schema: FullSchema) -> list[str]:
    return [finding.code for finding in schema.findings]


def find_one(node: FullNode, node_type: type[FullNode]) -> FullNode:
    if isinstance(node, node_type):
        return node
    for child in getattr(node, "children", ()):
        found = find_one(child, node_type)
        if found is not None:
            return found
    raise AssertionError(f"no {node_type.__name__}")


# ---------------------------------------------------------------------------
# Documentation shorthand
# ---------------------------------------------------------------------------


def test_documentation_comments_group_by_paragraph_and_attach_to_the_pattern() -> None:
    source = "## first line\n## second line\n\n## third\n\nelement root { empty }\n"
    schema = parse(source)

    element = schema.start
    assert isinstance(element, FullElement)
    documentation = elements_of(element)
    assert [annotation.name for annotation in documentation] == [
        "documentation",
        "documentation",
    ]
    assert {annotation.namespace for annotation in documentation} == {COMPAT_ANNOTATIONS}
    assert [
        annotation.element.text for annotation in documentation if annotation.element is not None
    ] == [
        "first line\nsecond line",
        "third",
    ]
    assert span_for(documentation[0]) == expected_span(source, "## first line\n## second line")
    assert span_for(documentation[1]) == expected_span(source, "## third")

    body = element.children[0]
    assert isinstance(body, FullEmpty)
    assert body.annotations == ()


def test_documentation_attaches_to_the_immediately_following_construct() -> None:
    schema = parse(
        "## schema\n"
        "start = element root {\n"
        "  ## body\n"
        "  text\n"
        "}\n"
        "## definition\n"
        "item = ## definition body\n"
        "  empty\n"
    )

    start = schema.start
    assert isinstance(start, FullGrammar)
    component, definition = start.scope.children
    assert isinstance(component, FullStart)
    assert elements_of(component)[0].element.text == "schema"  # type: ignore[union-attr]
    element = component.children[0]
    assert isinstance(element, FullElement)
    assert elements_of(element) == []
    pattern = element.children[0]
    assert elements_of(pattern)[0].element.text == "body"  # type: ignore[union-attr]
    assert isinstance(definition, FullDefine)
    assert elements_of(definition)[0].element.text == "definition"  # type: ignore[union-attr]
    body = definition.children[0]
    assert isinstance(body, FullEmpty)
    assert elements_of(body)[0].element.text == "definition body"  # type: ignore[union-attr]


def test_documentation_only_source_annotates_the_empty_grammar() -> None:
    schema = parse("## a schema without a body\n")

    assert isinstance(schema.start, FullGrammar)
    assert schema.start.scope.children == ()
    assert elements_of(schema.start)[0].element.text == "a schema without a body"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Initial annotations
# ---------------------------------------------------------------------------


def test_initial_annotation_attributes_and_children_attach_to_the_construct() -> None:
    source = (
        'namespace a = "urn:a"\n'
        'namespace b = "urn:b"\n'
        '[a:note = "root" b:flag = "yes" a:doc ["text"] a:child [b:id = "1"]] '
        "element root { empty }"
    )
    schema = parse(source)

    element = schema.start
    assert isinstance(element, FullElement)
    attributes = attributes_of(element)
    assert set(attributes) == {"note", "flag"}
    assert (attributes["note"].namespace, attributes["note"].value) == ("urn:a", "root")
    assert (attributes["flag"].namespace, attributes["flag"].value) == ("urn:b", "yes")
    assert element.name_class is not None
    assert element.name_class.annotations == ()
    body = element.children[0]
    assert isinstance(body, FullEmpty)
    assert body.annotations == ()

    document = element_annotation(element, "doc")
    assert document.element is not None
    assert document.element.tag == "{urn:a}doc"
    assert document.element.text == "text"
    child = element_annotation(element, "child")
    assert child.element is not None
    assert child.element.attrib == {"{urn:b}id": "1"}

    note = attributes["note"]
    note_location = origin_location(note)
    assert note_location.path == (PathStep("element", 1),)
    assert note_location.attribute == "{urn:a}note"
    assert note_location.span == expected_span(source, "a:note")
    assert origin_location(document).path == (
        PathStep("element", 1),
        PathStep("{urn:a}doc", 1),
    )
    assert span_for(document) == expected_span(source, "a:doc")


def test_repeated_annotation_elements_get_sibling_indices() -> None:
    schema = parse('namespace a = "urn:a"\n[a:note [] a:note []] element root { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    notes = elements_of(element)
    assert [origin_location(note).path for note in notes] == [
        (PathStep("element", 1), PathStep("{urn:a}note", 1)),
        (PathStep("element", 1), PathStep("{urn:a}note", 2)),
    ]


def test_empty_literal_content_is_not_emitted_as_text() -> None:
    schema = parse('namespace a = "urn:a"\n[a:doc ["before" "" a:inner []]] element root { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    document = element_annotation(element, "doc")
    assert document.element is not None
    assert document.element.text == "before"
    inner = document.element[0]
    assert inner.tail is None


def test_nested_foreign_elements_and_text_build_an_opaque_subtree() -> None:
    source = (
        'namespace a = "urn:a"\n'
        '[a:doc ["be" ~ "fore" a:inner [id = "1"] "after" a:empty []]] '
        "element root { empty }"
    )
    schema = parse(source)

    element = schema.start
    assert isinstance(element, FullElement)
    document = element_annotation(element, "doc")
    opaque = document.element
    assert opaque is not None
    assert opaque.text == "before"
    inner, empty = list(opaque)
    assert (inner.tag, inner.attrib) == ("{urn:a}inner", {"id": "1"})
    assert inner.tail == "after"
    assert empty.tag == "{urn:a}empty"
    assert schema.findings == ()


def test_initial_annotation_attributes_attach_to_a_data_pattern_and_param() -> None:
    schema = parse(
        'namespace a = "urn:a"\n'
        'element r { [a:d = "1"] xsd:string { [a:p = "2"] minLength = "1" } }'
    )

    data = find_one(schema.start, FullData)
    assert isinstance(data, FullData)
    assert attributes_of(data)["d"].value == "1"
    parameter = find_one(schema.start, FullParam)
    assert isinstance(parameter, FullParam)
    assert attributes_of(parameter)["p"].value == "2"


def test_annotation_before_parenthesized_particle_wraps_it_in_a_group() -> None:
    schema = parse('namespace a = "urn:a"\nelement r { [a:mark = "1"] (text) }')

    element = schema.start
    assert isinstance(element, FullElement)
    body = element.children[0]
    assert isinstance(body, FullGroup)
    assert attributes_of(body)["mark"].value == "1"
    assert isinstance(body.children[0], FullText)
    assert body.children[0].annotations == ()


def test_annotation_before_parenthesized_operator_attaches_to_the_container() -> None:
    schema = parse('namespace a = "urn:a"\nelement r { [a:mark = "1"] (text, empty) }')

    element = schema.start
    assert isinstance(element, FullElement)
    body = element.children[0]
    assert isinstance(body, FullGroup)
    assert attributes_of(body)["mark"].value == "1"
    assert [type(child) for child in body.children] == [FullText, FullEmpty]


def test_annotation_before_parenthesized_repetition_attaches_to_the_repetition() -> None:
    schema = parse('namespace a = "urn:a"\nelement r { [a:mark = "1"] (text*) }')

    element = schema.start
    assert isinstance(element, FullElement)
    body = element.children[0]
    assert isinstance(body, FullZeroOrMore)
    assert attributes_of(body)["mark"].value == "1"
    assert body.children[0].annotations == ()


def test_annotation_before_a_postfix_primary_attaches_to_the_primary() -> None:
    schema = parse('namespace a = "urn:a"\nelement r { [a:mark = "1"] text? }')

    element = schema.start
    assert isinstance(element, FullElement)
    optional = element.children[0]
    assert isinstance(optional, FullOptional)
    assert optional.annotations == ()
    assert attributes_of(optional.children[0])["mark"].value == "1"


def test_annotation_before_an_exception_wildcard_stays_on_the_wildcard() -> None:
    schema = parse('namespace a = "urn:a"\nelement [a:mark = "1"] * - x { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    name_class = element.name_class
    assert isinstance(name_class, FullAnyName)
    assert attributes_of(name_class)["mark"].value == "1"
    assert isinstance(name_class.except_class, FullNameExcept)
    excluded = name_class.except_class.alternatives[0]
    assert isinstance(excluded, FullName)
    assert excluded.lexical == "x"


def test_annotation_on_a_parenthesized_name_class_wraps_it_in_a_choice() -> None:
    schema = parse('namespace a = "urn:a"\nelement ([a:mark = "1"] (name)) { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    name_class = element.name_class
    assert isinstance(name_class, FullNameChoice)
    assert attributes_of(name_class)["mark"].value == "1"
    alternative = name_class.alternatives[0]
    assert isinstance(alternative, FullName)
    assert alternative.lexical == "name"


def test_annotation_on_a_parenthesized_name_class_choice_is_not_double_wrapped() -> None:
    schema = parse('namespace a = "urn:a"\nelement [a:mark = "1"] (x | y) { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    name_class = element.name_class
    assert isinstance(name_class, FullNameChoice)
    assert attributes_of(name_class)["mark"].value == "1"
    assert [type(alternative) for alternative in name_class.alternatives] == [
        FullName,
        FullName,
    ]
    assert [alternative.lexical for alternative in name_class.alternatives] == ["x", "y"]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Following annotations
# ---------------------------------------------------------------------------


def test_following_annotation_becomes_a_foreign_sibling_of_the_containing_element() -> None:
    source = 'namespace a = "urn:a"\nelement root { empty >> a:note [] }'
    schema = parse(source)

    element = schema.start
    assert isinstance(element, FullElement)
    note = element_annotation(element, "note")
    assert note.element is not None
    assert note.element.tag == "{urn:a}note"
    body = element.children[0]
    assert isinstance(body, FullEmpty)
    assert body.annotations == ()
    assert origin_location(note).path == (
        PathStep("element", 1),
        PathStep("{urn:a}note", 1),
    )
    assert span_for(note) == expected_span(source, "a:note")


def test_following_annotation_after_a_name_class_belongs_to_the_element() -> None:
    schema = parse('namespace a = "urn:a"\nelement * >> a:note [] { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class is not None
    assert element.name_class.annotations == ()
    assert element_annotation(element, "note").element is not None


def test_following_annotation_in_a_group_belongs_to_the_group() -> None:
    schema = parse('namespace a = "urn:a"\nelement root { (empty >> a:note []) , text }')

    element = schema.start
    assert isinstance(element, FullElement)
    assert element.annotations == ()
    group = element.children[0]
    assert isinstance(group, FullGroup)
    assert element_annotation(group, "note").element is not None
    assert isinstance(group.children[0], FullEmpty)
    assert group.children[0].annotations == ()


def test_following_annotation_before_and_after_a_postfix_uses_formal_placement() -> None:
    before = parse('namespace a = "urn:a"\nelement root { text >> a:note []? }')
    element = before.start
    assert isinstance(element, FullElement)
    assert element.annotations == ()
    optional = element.children[0]
    assert isinstance(optional, FullOptional)
    assert element_annotation(optional, "note").element is not None
    assert optional.children[0].annotations == ()

    after = parse('namespace a = "urn:a"\nelement root { text? >> a:note [] }')
    element = after.start
    assert isinstance(element, FullElement)
    assert element_annotation(element, "note").element is not None
    optional = element.children[0]
    assert isinstance(optional, FullOptional)
    assert optional.annotations == ()


def test_following_annotation_attributes_live_in_the_annotation_element() -> None:
    schema = parse('namespace a = "urn:a"\nelement root { empty >> a:note [xml:lang = "en"] }')

    element = schema.start
    assert isinstance(element, FullElement)
    note = element_annotation(element, "note")
    assert note.element is not None
    assert note.element.attrib == {f"{{{XML_NS}}}lang": "en"}


def test_top_level_following_annotation_violates_the_single_element_constraint() -> None:
    source = 'namespace a = "urn:a"\nempty >> a:note []\n'
    schema = parse(source)

    assert isinstance(schema.start, FullEmpty)
    assert codes(schema) == ["relaxng-compact-single-element"]
    finding = schema.findings[0]
    assert finding.severity == "error"
    assert finding.location.span == expected_span(source, "a:note")


# ---------------------------------------------------------------------------
# Grammar and include-content annotations
# ---------------------------------------------------------------------------


def test_grammar_annotations_and_annotated_components_keep_their_place() -> None:
    source = (
        'namespace a = "urn:a"\n'
        '[a:meta = "one"] start = empty\n'
        'a:entity [ name = "picture" systemId = "picture.jpeg" ]\n'
        'a:extra ["text"]\n'
    )
    schema = parse(source)

    grammar = schema.start
    assert isinstance(grammar, FullGrammar)
    component = grammar.scope.children[0]
    assert isinstance(component, FullStart)
    assert attributes_of(component)["meta"].value == "one"
    assert grammar.scope.annotations == grammar.annotations
    entity = element_annotation(grammar, "entity")
    assert entity.element is not None
    assert entity.element.attrib == {"name": "picture", "systemId": "picture.jpeg"}
    extra = element_annotation(grammar, "extra")
    assert extra.element is not None
    assert extra.element.text == "text"
    assert annotation_path(entity)[0] == ("grammar", 1)


def test_include_content_and_include_annotations_attach_to_the_placeholder() -> None:
    schema = parse(
        'namespace a = "urn:a"\n'
        '[a:inc = "1"] include "inc.rnc" {\n'
        '  [a:ov = "2"] item = empty\n'
        '  a:note ["included"]\n'
        "}\n"
    )

    grammar = schema.start
    assert isinstance(grammar, FullGrammar)
    include = grammar.scope.children[0]
    assert isinstance(include, ReferencePlaceholder)
    assert (include.kind, include.href) == ("include", "inc.rnc")
    assert attributes_of(include)["inc"].value == "1"
    assert element_annotation(include, "note").element is not None
    override = include.children[0]
    assert isinstance(override, FullDefine)
    assert attributes_of(override)["ov"].value == "2"


def test_nested_grammar_takes_annotations_of_its_own_content() -> None:
    schema = parse(
        'namespace a = "urn:a"\nelement r { [a:g = "1"] grammar { a:note ["inner"] start = text } }'
    )

    element = schema.start
    assert isinstance(element, FullElement)
    inner = element.children[0]
    assert isinstance(inner, FullGrammar)
    assert attributes_of(inner)["g"].value == "1"
    assert element_annotation(inner, "note").element is not None
    assert inner.scope.annotations == inner.annotations


# ---------------------------------------------------------------------------
# Quoted keyword names
# ---------------------------------------------------------------------------


def test_quoted_keywords_work_as_annotation_names() -> None:
    schema = parse('namespace a = "urn:a"\n[\\element ["x"]] element root { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    quoted = elements_of(element)[0]
    assert (quoted.name, quoted.namespace) == ("element", None)


def test_keywords_work_as_nested_annotation_attribute_names() -> None:
    schema = parse('namespace a = "urn:a"\n[a:doc [element = "v"]] element root { empty }')

    element = schema.start
    assert isinstance(element, FullElement)
    document = element_annotation(element, "doc")
    assert document.element is not None
    assert document.element.attrib == {"element": "v"}


def test_quoted_keyword_grammar_annotation_element_is_accepted() -> None:
    schema = parse('namespace a = "urn:a"\n\\start [a:x = "1"]\nstart = empty\n')

    grammar = schema.start
    assert isinstance(grammar, FullGrammar)
    names = [annotation.name for annotation in elements_of(grammar)]
    assert names == ["start"]


# ---------------------------------------------------------------------------
# Recoverable constraints
# ---------------------------------------------------------------------------


def test_duplicate_declarations_report_the_second_declaration_and_keep_the_first() -> None:
    source = (
        'namespace p = "urn:first"\n'
        'namespace p = "urn:second"\n'
        'default namespace = "urn:d1"\n'
        'default namespace = "urn:d2"\n'
        'datatypes d = "urn:dt1"\n'
        'datatypes d = "urn:dt2"\n'
        "element root { d:thing }"
    )
    schema = parse(source)

    duplicate_p = source.index('namespace p = "urn:second"') + len("namespace ")
    duplicate_default = source.index('default namespace = "urn:d2"') + len("default ")
    duplicate_d = source.index('datatypes d = "urn:dt2"') + len("datatypes ")
    assert codes(schema) == ["relaxng-compact-duplicate-declaration"] * 3
    assert [finding.location.span for finding in schema.findings] == [
        expected_span_at(source, duplicate_p, duplicate_p + 1),
        expected_span_at(source, duplicate_default, duplicate_default + len("namespace")),
        expected_span_at(source, duplicate_d, duplicate_d + 1),
    ]
    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class is not None
    assert element.name_class.uri == "urn:d1"  # type: ignore[union-attr]
    data = element.children[0]
    assert isinstance(data, FullData)
    assert data.datatype_library == "urn:dt1"
    assert schema.resource.namespaces.root_bindings["p"] == "urn:first"


def test_default_namespace_prefix_counts_as_a_prefix_declaration() -> None:
    source = 'default namespace ex = "urn:e"\nnamespace ex = "urn:other"\nelement root { empty }'
    schema = parse(source)

    assert codes(schema) == ["relaxng-compact-duplicate-declaration"]
    assert schema.findings[0].location.span == expected_span(source, "ex", 1)


def test_default_namespace_with_the_xmlns_prefix_is_rejected() -> None:
    schema = parse('default namespace xmlns = "urn:x"\nelement root { empty }')

    assert codes(schema) == ["relaxng-compact-invalid-prefix"]
    assert "xmlns" not in schema.resource.namespaces.root_bindings
    element = schema.start
    assert isinstance(element, FullElement)
    assert element.name_class is not None
    assert element.name_class.uri is None  # type: ignore[union-attr]


def test_default_namespace_prefix_duplicate_is_reported() -> None:
    source = (
        'namespace ex = "urn:first"\ndefault namespace ex = "urn:other"\nelement root { empty }'
    )
    schema = parse(source)

    assert codes(schema) == ["relaxng-compact-duplicate-declaration"]
    duplicate = source.index('default namespace ex = "urn:other"') + len("default namespace ")
    assert schema.findings[0].location.span == expected_span_at(source, duplicate, duplicate + 2)
    assert schema.resource.namespaces.root_bindings["ex"] == "urn:first"


def test_reserved_prefix_and_namespace_uri_constraints() -> None:
    invalid = parse('namespace xmlns = "urn:x"\nelement root { empty }')
    assert codes(invalid) == ["relaxng-compact-invalid-prefix"]
    assert invalid.findings[0].location.span == expected_span(
        'namespace xmlns = "urn:x"\nelement root { empty }', "xmlns"
    )
    assert "xmlns" not in invalid.resource.namespaces.root_bindings

    wrong_xml = parse('namespace xml = "urn:wrong"\nelement root { empty }')
    assert codes(wrong_xml) == ["relaxng-compact-xml-prefix"]
    assert wrong_xml.resource.namespaces.root_bindings["xml"] == XML_NS

    xml_uri = parse(f'namespace p = "{XML_NS}"\nelement root {{ empty }}')
    assert codes(xml_uri) == ["relaxng-compact-xml-namespace-uri"]

    default_xml_uri = parse(f'default namespace = "{XML_NS}"\nelement root {{ empty }}')
    assert codes(default_xml_uri) == ["relaxng-compact-xml-namespace-uri"]

    prefixed_default = parse('default namespace xml = "urn:x"\nelement root { empty }')
    assert codes(prefixed_default) == ["relaxng-compact-xml-prefix"]


def test_xsd_and_datatypes_uri_constraints() -> None:
    source = 'datatypes xsd = "urn:wrong"\ndatatypes d = "relative"\nelement root { xsd:string }'
    schema = parse(source)

    assert codes(schema) == [
        "relaxng-compact-xsd-prefix",
        "relaxng-compact-datatypes-uri",
    ]
    xsd_start = source.index("xsd =")
    assert schema.findings[0].location.span == expected_span_at(
        source, xsd_start, xsd_start + len("xsd")
    )
    assert schema.findings[1].location.span == expected_span(source, '"relative"')
    element = schema.start
    assert isinstance(element, FullElement)
    data = element.children[0]
    assert isinstance(data, FullData)
    assert data.datatype_library == XSD_DATATYPES


def test_unbound_and_inherited_annotation_prefixes_become_findings() -> None:
    unbound = parse('[p:note = "v"] element root { empty }')
    assert codes(unbound) == ["relaxng-compact-unbound-prefix"]
    assert unbound.findings[0].location.span == expected_span(
        '[p:note = "v"] element root { empty }', "p:note"
    )
    element = unbound.start
    assert isinstance(element, FullElement)
    note = attributes_of(element)["note"]
    assert (note.namespace, note.value) == (None, "v")

    inherited = parse("namespace p = inherit\n[p:note []] element root { empty }")
    assert codes(inherited) == ["relaxng-compact-annotation-inherit"]
    element = inherited.start
    assert isinstance(element, FullElement)
    assert elements_of(element)[0].namespace is None

    nested = parse('namespace a = "urn:a"\n[a:doc [p:x = "1"]] element root { empty }')
    assert codes(nested) == ["relaxng-compact-unbound-prefix"]


def test_annotation_names_may_not_use_the_relax_ng_namespace() -> None:
    reserved = parse(f'namespace r = "{RELAX_NG_NAMESPACE}"\n[r:note []] element root {{ empty }}')
    assert codes(reserved) == ["relaxng-compact-reserved-namespace"]

    attribute = parse(
        f'namespace r = "{RELAX_NG_NAMESPACE}"\n[r:note = "1"] element root {{ empty }}'
    )
    assert codes(attribute) == ["relaxng-compact-reserved-namespace"]

    nested = parse(
        'namespace a = "urn:a"\n'
        f'namespace r = "{RELAX_NG_NAMESPACE}"\n'
        "[a:doc [r:inner []]] element root { empty }"
    )
    assert nested.findings == ()


def test_annotation_attribute_names_reject_unqualified_and_xmlns_names() -> None:
    unqualified = parse('namespace e = ""\n[e:note = "1"] element root { empty }')
    assert codes(unqualified) == ["relaxng-compact-unqualified-attribute-name"]
    element = unqualified.start
    assert isinstance(element, FullElement)
    assert attributes_of(element)["note"].namespace is None

    xmlns = parse(f'namespace x = "{XMLNS_NAMESPACE}"\n[x:note = "1"] element root {{ empty }}')
    assert codes(xmlns) == ["relaxng-compact-xmlns-namespace"]

    nested = parse(
        'namespace a = "urn:a"\n'
        f'namespace x = "{XMLNS_NAMESPACE}"\n'
        '[a:doc [x:note = "1"]] element root { empty }'
    )
    assert codes(nested) == ["relaxng-compact-xmlns-namespace"]


def test_duplicate_annotation_attributes_keep_the_first_value() -> None:
    duplicated = parse('namespace a = "urn:a"\n[a:x = "1" a:x = "2"] element root { empty }')
    assert codes(duplicated) == ["relaxng-compact-duplicate-attribute"]
    element = duplicated.start
    assert isinstance(element, FullElement)
    assert attributes_of(element)["x"].value == "1"

    nested = parse('namespace a = "urn:a"\n[a:doc [x = "1" x = "2"]] element root { empty }')
    assert codes(nested) == ["relaxng-compact-duplicate-attribute"]
    element = nested.start
    assert isinstance(element, FullElement)
    document = element_annotation(element, "doc")
    assert document.element is not None
    assert document.element.attrib == {"x": "1"}


# ---------------------------------------------------------------------------
# Top-level disambiguation and annotation errors
# ---------------------------------------------------------------------------


def test_leading_annotations_are_seen_through_for_top_level_disambiguation() -> None:
    grammar = parse('namespace a = "urn:a"\n[a:kind = "grammar"] start = text\n')
    assert isinstance(grammar.start, FullGrammar)
    component = grammar.start.scope.children[0]
    assert isinstance(component, FullStart)
    assert attributes_of(component)["kind"].value == "grammar"

    pattern = parse('namespace a = "urn:a"\n[a:kind = "pattern"] element root { empty }\n')
    assert isinstance(pattern.start, FullElement)
    assert attributes_of(pattern.start)["kind"].value == "pattern"


def test_annotation_element_member_cannot_have_initial_annotations() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse('namespace a = "urn:a"\n[a:x = "1"] a:note []\n')

    assert raised.value.found == "'a:note'"
    assert "initial annotations" in str(raised.value)


def test_unterminated_leading_annotation_is_rejected() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as raised:
        parse('[a:x = "1"')

    assert raised.value.found == "end of input"
    assert raised.value.expected == ("']'",)


def test_follow_annotation_requires_an_annotation_element() -> None:
    with pytest.raises(RelaxNGCompactSyntaxError) as missing:
        parse("element r { empty >> }")
    assert missing.value.found == "'}'"
    assert "annotation element" in str(missing.value)

    with pytest.raises(RelaxNGCompactSyntaxError) as no_content:
        parse("element r { empty >> a:note }")
    assert no_content.value.found == "'}'"
    assert no_content.value.expected == ("'['",)


# ---------------------------------------------------------------------------
# Depth budget
# ---------------------------------------------------------------------------


def test_nested_annotation_brackets_observe_the_depth_high_water_mark() -> None:
    source = "element r { [x []] text }"

    parse(source, limits=RelaxNGLimits(max_compact_depth=3))
    with pytest.raises(RelaxNGLimitError) as raised:
        parse(source, limits=RelaxNGLimits(max_compact_depth=2))
    assert raised.value.budget == "max_compact_depth"
    assert raised.value.observed == 3

    # Sibling annotation brackets both reach depth two and do not accumulate.
    parse("element r { [] text , [] text }", limits=RelaxNGLimits(max_compact_depth=2))
