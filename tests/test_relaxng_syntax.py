"""Full-syntax RELAX NG reading with unresolved scopes and placeholders.

The reader builds the authored grammar tree without deciding grammar
legality.  It preserves every construct needed by simplification: grammar
scopes with their parent links, start/define/combine components, unresolved
reference placeholders, name classes, datatype-library context, annotations,
findings for unknown syntax, and exact source origins.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS
from pyxsd.relaxng.origins import SourceOrigin
from pyxsd.relaxng.resources import LoadedResource, ResourceLoader
from pyxsd.relaxng.syntax import (
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
    FullMixed,
    FullName,
    FullNameChoice,
    FullNameClass,
    FullNameExcept,
    FullNode,
    FullNotAllowed,
    FullNsName,
    FullParam,
    FullSchema,
    FullStart,
    FullText,
    FullUnknown,
    FullValue,
    ReferencePlaceholder,
    SchemaFinding,
    read_full_syntax,
)

FIXTURES = Path(__file__).parent / "relaxng" / "fixtures"
RNG_NS = "http://relaxng.org/ns/structure/1.0"
TAG = f"{{{RNG_NS}}}"


def read_schema(xml: str, *, name: str | None = None) -> FullSchema:
    stream = io.StringIO(xml)
    if name is not None:
        stream.name = name
    return read_full_syntax(ResourceLoader().load(stream))


def read_fixture(relative: str) -> FullSchema:
    return read_full_syntax(ResourceLoader().load(FIXTURES / relative))


def walk_name_class(name_class: FullNameClass) -> Iterator[FullNode]:
    yield name_class
    if isinstance(name_class, (FullNameChoice, FullNameExcept)):
        for alternative in name_class.alternatives:
            yield from walk_name_class(alternative)
    if isinstance(name_class, (FullAnyName, FullNsName)) and name_class.except_class:
        yield from walk_name_class(name_class.except_class)


def walk(node: FullNode) -> Iterator[FullNode]:
    yield node
    if isinstance(node, FullGrammar):
        for component in node.scope.children:
            yield from walk(component)
        return
    for child in getattr(node, "children", ()):
        if isinstance(child, FullNode):
            yield from walk(child)
    name_class = getattr(node, "name_class", None)
    if isinstance(name_class, FullNameClass):
        yield from walk_name_class(name_class)


def find_all(schema: FullSchema, node_type: type[FullNode]) -> list[FullNode]:
    return [node for node in walk(schema.start) if isinstance(node, node_type)]


def find_one(schema: FullSchema, node_type: type[FullNode]) -> FullNode:
    return next(node for node in walk(schema.start) if isinstance(node, node_type))


def codes(schema: FullSchema) -> list[str]:
    return [finding.code for finding in schema.findings]


class TestGrammarScope:
    def test_start_define_combine_and_ref(self):
        schema = read_fixture("grammar.rng")

        assert schema.findings == ()
        assert isinstance(schema.start, FullGrammar)
        scope = schema.start.scope
        assert schema.grammars == (scope,)
        assert scope.parent is None
        assert repr(scope) == "GrammarScope(starts=1, definitions=1, includes=0, divs=0)"
        assert len(scope.starts) == 1

        start = scope.starts[0]
        assert start.combine == "choice"
        ref = start.children[0]
        assert isinstance(ref, ReferencePlaceholder)
        assert ref.kind == "ref"
        assert ref.name == "root"
        assert ref.scope is scope
        assert ref.href is None

        define = scope.definitions[0]
        assert define.name == "root"
        assert define.combine == "interleave"
        element = define.children[0]
        assert isinstance(element, FullElement)
        assert element.name_class is not None
        assert element.name_class.uri is None
        assert element.name_class.local == "doc"
        assert any(isinstance(node, FullMixed) for node in walk(element))

    def test_bare_pattern_is_a_start_without_a_grammar_scope(self):
        schema = read_schema(f'<element xmlns="{RNG_NS}" name="root"><empty/></element>')

        assert isinstance(schema.start, FullElement)
        assert schema.grammars == ()
        assert schema.start.name_class is not None
        assert schema.start.name_class.local == "root"

    def test_non_pattern_relax_ng_root_is_reported(self):
        schema = read_schema(f'<start xmlns="{RNG_NS}"><empty/></start>')

        assert codes(schema) == ["relaxng-root-not-pattern"]
        assert isinstance(schema.start, FullUnknown)
        assert schema.start.tag == f"{{{RNG_NS}}}start"

    def test_grammar_components_inside_pattern_content_are_preserved(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <start><empty/></start>
              <define name="d"><empty/></define>
              <include href="part.rng"/>
              <name>unexpected</name>
              <param name="p">v</param>
              <except><text/></except>
            </element>
            """
        )

        element = schema.start
        assert isinstance(element, FullElement)
        assert [type(child) for child in element.children] == [
            FullStart,
            FullDefine,
            ReferencePlaceholder,
            FullName,
            FullParam,
            FullExcept,
        ]
        include = element.children[2]
        assert isinstance(include, ReferencePlaceholder)
        assert include.kind == "include"

    def test_div_and_not_allowed_patterns_are_preserved(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <div><notAllowed/></div>
            </element>
            """
        )

        element = schema.start
        assert isinstance(element, FullElement)
        div = element.children[0]
        assert isinstance(div, FullDiv)
        assert isinstance(div.children[0], FullNotAllowed)

    def test_multiple_starts_and_definitions_are_preserved_in_order(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><empty/></start>
              <start combine="interleave"><text/></start>
              <define name="one"><empty/></define>
              <define name="two"><text/></define>
            </grammar>
            """
        )

        scope = schema.start.scope
        assert [start.combine for start in scope.starts] == [None, "interleave"]
        assert [define.name for define in scope.definitions] == ["one", "two"]

    def test_nested_grammar_preserves_parent_scope_context(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start>
                <grammar>
                  <start><ref name="inner"/></start>
                  <define name="inner">
                    <element name="i"><empty/></element>
                  </define>
                </grammar>
              </start>
            </grammar>
            """
        )

        outer = schema.start.scope
        assert len(schema.grammars) == 2
        inner = schema.grammars[1]
        assert inner.parent is outer
        nested = outer.starts[0].children[0]
        assert isinstance(nested, FullGrammar)
        assert nested.scope is inner
        ref = inner.starts[0].children[0]
        assert isinstance(ref, ReferencePlaceholder)
        assert ref.scope is inner

    def test_div_components_are_preserved(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <div>
                <start><empty/></start>
                <define name="d"><text/></define>
              </div>
            </grammar>
            """
        )

        scope = schema.start.scope
        assert scope.starts == ()
        assert len(scope.divs) == 1
        div = scope.divs[0]
        assert [type(child) for child in div.children] == [FullStart, FullDefine]
        assert scope.children == (div,)

    def test_pattern_children_in_grammar_content_are_preserved_as_extras(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <element name="late"><empty/></element>
              <start><empty/></start>
            </grammar>
            """
        )

        scope = schema.start.scope
        assert len(scope.extras) == 1
        assert isinstance(scope.extras[0], FullElement)
        assert scope.children[0] is scope.extras[0]
        assert scope.starts


class TestPlaceholders:
    def test_include_and_external_ref_placeholders(self):
        schema = read_fixture("external/main.rng")
        scope = schema.start.scope
        resource = schema.resource

        include = scope.includes[0]
        assert include.kind == "include"
        assert include.href == "parts/included.rng"
        assert include.base == resource.canonical_id
        assert include.scope is scope
        assert include.ns_attribute is None
        assert len(include.children) == 1
        override = include.children[0]
        assert isinstance(override, FullDefine)
        assert override.name == "override"

        external = scope.starts[0].children[0]
        assert isinstance(external, ReferencePlaceholder)
        assert external.kind == "externalRef"
        assert external.href == "parts/child.rng"
        assert external.base == resource.canonical_id
        assert external.ns_attribute is None
        assert external.name is None

    def test_xml_base_directly_on_include_and_external_ref(self, tmp_path):
        main = tmp_path / "main.rng"
        main.write_text(
            f"""
            <grammar xmlns="{RNG_NS}">
              <include xml:base="inc/" href="included.rng"/>
              <start>
                <externalRef xml:base="parts/" href="child.rng"/>
              </start>
            </grammar>
            """,
            encoding="utf-8",
        )

        schema = read_full_syntax(ResourceLoader().load(main))
        scope = schema.start.scope

        assert scope.includes[0].base == str(tmp_path / "inc") + "/"
        external = scope.starts[0].children[0]
        assert isinstance(external, ReferencePlaceholder)
        assert external.base == str(tmp_path / "parts") + "/"

    def test_dot_xml_base_resolves_in_the_containing_directory(self, tmp_path):
        schema_dir = tmp_path / "a"
        schema_dir.mkdir()
        target = schema_dir / "child.rng"
        target.write_text(
            f'<element xmlns="{RNG_NS}" name="child"><empty/></element>',
            encoding="utf-8",
        )
        decoy = tmp_path / "child.rng"
        decoy.write_text(
            f'<element xmlns="{RNG_NS}" name="decoy"><empty/></element>',
            encoding="utf-8",
        )
        main = schema_dir / "main.rng"
        main.write_text(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start>
                <externalRef xml:base="." href="child.rng"/>
              </start>
            </grammar>
            """,
            encoding="utf-8",
        )

        loader = ResourceLoader()
        resource = loader.load(main)
        schema = read_full_syntax(resource)
        external = find_one(schema, ReferencePlaceholder)
        assert isinstance(external, ReferencePlaceholder)

        assert external.base == str(schema_dir) + "/"
        loaded = loader.resolve(external.href or "", resource, external.base)
        assert loaded.canonical_id == str(target.resolve())
        assert loaded.root.get("name") == "child"

    def test_placeholder_keeps_its_own_ns_attribute(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <include href="part.rng" ns="urn:include"/>
              <start ns="urn:outer">
                <externalRef href="external.rng" ns="urn:external"/>
              </start>
            </grammar>
            """
        )

        scope = schema.start.scope
        assert scope.includes[0].ns_attribute == "urn:include"
        external = scope.starts[0].children[0]
        assert isinstance(external, ReferencePlaceholder)
        assert external.ns_attribute == "urn:external"

    def test_xml_base_inheritance_reaches_placeholders(self):
        schema = read_fixture("nested/base.rng")
        scope = schema.start.scope
        start = scope.starts[0]

        inner_element = start.children[0]
        assert isinstance(inner_element, FullElement)
        nested_ref = inner_element.children[0]
        assert isinstance(nested_ref, ReferencePlaceholder)
        assert nested_ref.base.endswith("parts/sub/")
        assert nested_ref.base != schema.resource.canonical_id

        sibling_ref = start.children[1]
        assert isinstance(sibling_ref, ReferencePlaceholder)
        assert sibling_ref.base.endswith("parts/")

        loader = ResourceLoader()
        nested_target = loader.resolve(nested_ref.href or "", schema.resource, nested_ref.base)
        sibling_target = loader.resolve(sibling_ref.href or "", schema.resource, sibling_ref.base)
        assert nested_target.canonical_id == str(
            (FIXTURES / "nested/parts/sub/child.rng").resolve()
        )
        assert sibling_target.canonical_id == str((FIXTURES / "nested/parts/other.rng").resolve())

    def test_parent_ref_and_ref_outside_grammar(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <group>
                <ref name="same"/>
                <parentRef name="outer"/>
              </group>
            </element>
            """
        )

        refs = find_all(schema, ReferencePlaceholder)
        assert [ref.kind for ref in refs] == ["ref", "parentRef"]
        assert refs[0].scope is None
        assert refs[1].scope is None

    def test_missing_hrefs_and_names_are_findings_not_crashes(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start>
                <group>
                  <ref/>
                  <parentRef/>
                  <externalRef/>
                </group>
              </start>
              <define><empty/></define>
              <include/>
            </grammar>
            """
        )

        missing = [
            finding for finding in schema.findings if finding.code == "relaxng-missing-attribute"
        ]
        assert len(missing) == 5
        placeholders = find_all(schema, ReferencePlaceholder)
        assert {placeholder.kind for placeholder in placeholders} == {
            "ref",
            "parentRef",
            "externalRef",
            "include",
        }
        assert all(placeholder.name is None for placeholder in placeholders[:3])
        include = next(placeholder for placeholder in placeholders if placeholder.kind == "include")
        assert include.href is None
        assert all(finding.location.attribute for finding in missing)

    def test_relative_xml_base_from_an_anonymous_stream_is_a_finding(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start xml:base="parts/">
                <externalRef href="child.rng"/>
              </start>
            </grammar>
            """
        )

        assert "relaxng-invalid-base" in codes(schema)
        finding = next(
            finding for finding in schema.findings if finding.code == "relaxng-invalid-base"
        )
        assert finding.location.attribute == f"{{{XML_NS}}}base"
        ref = find_one(schema, ReferencePlaceholder)
        assert isinstance(ref, ReferencePlaceholder)
        assert ref.base == "<memory>"


class TestNames:
    def test_element_names_use_relax_ng_namespace_inheritance(self):
        schema = read_fixture("namespaces.rng")
        elements = find_all(schema, FullElement)
        names = {
            (element.name_class.uri, element.name_class.local)
            for element in elements
            if isinstance(element.name_class, FullName)
        }

        assert ("urn:example:p", "root") in names
        assert ("urn:example:inherited", "child") in names
        assert ("urn:example:inherited", "inner") in names
        assert (None, "unqualified") in names

    def test_attribute_names_do_not_inherit_ns_without_an_own_attribute(self):
        schema = read_fixture("namespaces.rng")
        attributes = {
            attribute.name_class.local: attribute.name_class.uri
            for attribute in find_all(schema, FullAttribute)
            if attribute.name_class is not None
        }

        assert attributes["plain"] is None
        assert attributes["marked"] == "urn:example:p"
        assert attributes["named"] == "urn:example:attribute"

    def test_name_class_choice_any_name_and_except_forms(self):
        schema = read_fixture("namespaces.rng")
        elements = find_all(schema, FullElement)

        choice_element = next(
            element for element in elements if isinstance(element.name_class, FullNameChoice)
        )
        choice = choice_element.name_class
        assert isinstance(choice, FullNameChoice)
        assert len(choice.alternatives) == 2
        first, second = choice.alternatives
        assert isinstance(first, FullName)
        assert (first.uri, first.local) == ("urn:example:a", "first")
        assert isinstance(second, FullNsName)
        assert second.uri is None
        assert isinstance(second.except_class, FullNameExcept)
        excluded = second.except_class.alternatives[0]
        assert isinstance(excluded, FullName)
        assert (excluded.uri, excluded.local) == (None, "second")

        any_name_element = next(
            element for element in elements if isinstance(element.name_class, FullAnyName)
        )
        any_name = any_name_element.name_class
        assert isinstance(any_name, FullAnyName)
        assert isinstance(any_name.except_class, FullNameExcept)
        excluded_ns = any_name.except_class.alternatives[0]
        assert isinstance(excluded_ns, FullNsName)
        assert excluded_ns.uri == "urn:example:excluded"

    def test_unbound_prefix_is_a_finding_without_a_namespace(self):
        schema = read_schema(f'<element xmlns="{RNG_NS}" name="missing:root"><empty/></element>')

        element = schema.start
        assert isinstance(element, FullElement)
        assert element.name_class is not None
        assert element.name_class.lexical == "missing:root"
        assert element.name_class.uri is None
        assert element.name_class.local == "root"
        finding = next(
            finding for finding in schema.findings if finding.code == "relaxng-unbound-prefix"
        )
        assert finding.location.attribute == "name"

    def test_xml_prefix_is_implicitly_bound(self):
        schema = read_schema(
            f'<element xmlns="{RNG_NS}" name="root">'
            '<attribute name="xml:lang"><empty/></attribute>'
            "</element>"
        )

        attribute = find_one(schema, FullAttribute)
        assert isinstance(attribute, FullAttribute)
        assert attribute.name_class is not None
        assert attribute.name_class.uri == XML_NS
        assert attribute.name_class.local == "lang"

    def test_prefixed_name_wins_over_inherited_ns(self):
        schema = read_schema(
            f'<element xmlns="{RNG_NS}" xmlns:p="urn:p" name="p:root" ns="urn:ignored">'
            "<empty/></element>"
        )

        element = schema.start
        assert isinstance(element, FullElement)
        assert element.name_class is not None
        assert (element.name_class.uri, element.name_class.local) == ("urn:p", "root")

    def test_nsname_uses_its_own_or_inherited_ns(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}" ns="urn:outer">
              <start>
                <element>
                  <choice>
                    <nsName/>
                    <nsName ns="urn:own"/>
                  </choice>
                  <empty/>
                </element>
              </start>
            </grammar>
            """
        )

        elements = find_all(schema, FullElement)
        name_class = elements[0].name_class
        assert isinstance(name_class, FullNameChoice)
        inherited, own = name_class.alternatives
        assert isinstance(inherited, FullNsName)
        assert inherited.uri == "urn:outer"
        assert isinstance(own, FullNsName)
        assert own.uri == "urn:own"

    def test_name_element_content_is_trimmed_for_resolution(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}">
              <name>  spaced  </name>
              <empty/>
            </element>
            """
        )

        element = schema.start
        assert isinstance(element, FullElement)
        assert element.name_class is not None
        assert element.name_class.lexical == "  spaced  "
        assert element.name_class.local == "spaced"
        assert element.name_class.uri is None


class TestDatatypes:
    def test_datatype_library_inheritance_and_value_defaults(self):
        schema = read_fixture("datatypes.rng")
        data = find_one(schema, FullData)
        assert isinstance(data, FullData)
        assert data.datatype_library == "urn:example:datatypes"
        assert data.type_name == "integer"
        assert data.base_uri == schema.resource.canonical_id
        assert dict(data.namespaces)[""] == RNG_NS
        assert [(parameter.name, parameter.value) for parameter in data.parameters] == [
            ("minInclusive", " 1 ")
        ]

        values = {
            value.lexical: value
            for value in find_all(schema, FullValue)
            if isinstance(value, FullValue)
        }
        assert values[" fixed "].datatype_library == "urn:example:datatypes"
        assert values[" fixed "].type_name == "token"
        assert values[" fixed "].base_uri == schema.resource.canonical_id
        assert values["raw"].datatype_library == ""
        assert values["raw"].type_name is None
        assert values["0"].datatype_library == "urn:example:datatypes"

    def test_data_except_pattern_is_preserved(self):
        schema = read_fixture("datatypes.rng")
        datas = [data for data in find_all(schema, FullData) if isinstance(data, FullData)]
        decimal = next(data for data in datas if data.type_name == "decimal")

        assert decimal.parameters == ()
        assert isinstance(decimal.except_pattern, FullExcept)
        excluded = decimal.except_pattern.children[0]
        assert isinstance(excluded, FullValue)
        assert excluded.lexical == "0"

    def test_value_namespace_uri_and_prefix_frame(self):
        schema = read_fixture("namespaces.rng")
        value = find_one(schema, FullValue)
        assert isinstance(value, FullValue)

        assert value.namespace_uri == "urn:example:inherited"
        bindings = dict(value.namespaces)
        assert bindings["p"] == "urn:example:p"
        assert bindings[""] == ""
        assert "urn:example:default" not in bindings.values()

    def test_text_content_of_values_and_parameters_is_preserved(self):
        schema = read_fixture("datatypes.rng")
        value = next(
            value
            for value in find_all(schema, FullValue)
            if isinstance(value, FullValue) and value.lexical == " fixed "
        )
        parameter = find_one(schema, FullParam)
        assert isinstance(parameter, FullParam)

        assert value.lexical == " fixed "
        assert parameter.value == " 1 "


class TestAnnotations:
    def test_annotations_are_recorded_but_do_not_change_patterns(self):
        schema = read_fixture("annotations.rng")

        assert schema.findings == ()
        scope = schema.start.scope
        assert any(
            annotation.kind == "element"
            and annotation.namespace == "urn:example:annotation"
            and annotation.name == "documentation"
            for annotation in scope.annotations
        )
        assert {
            annotation.name for annotation in scope.annotations if annotation.kind == "attribute"
        } == {"note", "lang"}

        element = find_one(schema, FullElement)
        assert isinstance(element, FullElement)
        assert [type(child) for child in element.children] == [FullEmpty]
        assert any(
            annotation.kind == "element" and annotation.name == "annotation"
            for annotation in element.annotations
        )
        assert any(
            annotation.kind == "element"
            and annotation.namespace == "urn:example:annotation"
            and annotation.name == "aside"
            for annotation in element.annotations
        )
        assert {
            annotation.name for annotation in element.annotations if annotation.kind == "attribute"
        } == {"mark"}

    def test_annotation_content_is_opaque(self):
        schema = read_fixture("annotations.rng")
        scope = schema.start.scope

        assert scope.definitions == ()
        assert "relaxng-unknown-element" not in codes(schema)

    def test_foreign_root_is_a_finding_and_an_unknown_pattern(self):
        schema = read_fixture("foreign-root.rng")

        assert codes(schema) == ["relaxng-root-not-relaxng"]
        assert isinstance(schema.start, FullUnknown)
        assert schema.start.tag == "{urn:example:html}html"

    def test_relax_ng_annotation_element_is_unknown_and_preserved(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" xmlns:a="urn:example:annotation">
              <name>root</name>
              <annotation a:note="x">
                <define name="ignored"><empty/></define>
                <bogus/>
              </annotation>
              <empty/>
            </element>
            """
        )

        assert "relaxng-unknown-element" in codes(schema)
        assert "relaxng-missing-name-class" not in codes(schema)
        element = schema.start
        assert isinstance(element, FullElement)
        assert element.name_class is not None
        assert element.name_class.local == "root"
        assert len(element.children) == 2
        unknown = element.children[0]
        assert isinstance(unknown, FullUnknown)
        assert unknown.tag == f"{{{RNG_NS}}}annotation"
        assert any(
            annotation.kind == "attribute" and annotation.name == "note"
            for annotation in unknown.annotations
        )
        assert find_all(schema, FullDefine) == []

    def test_relax_ng_annotation_does_not_hide_a_name_class(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}">
              <annotation><empty/></annotation>
              <name>root</name>
              <empty/>
            </element>
            """
        )

        assert "relaxng-missing-name-class" not in codes(schema)
        element = schema.start
        assert isinstance(element, FullElement)
        assert element.name_class is not None
        assert element.name_class.local == "root"
        assert any(isinstance(child, FullUnknown) for child in element.children)

    def test_relax_ng_annotation_root_is_not_a_pattern(self):
        schema = read_schema(f'<annotation xmlns="{RNG_NS}"><text/></annotation>')

        assert codes(schema) == ["relaxng-root-not-pattern"]
        assert isinstance(schema.start, FullUnknown)


class TestFindings:
    def test_unknown_elements_and_attributes_are_reported_and_preserved(self):
        schema = read_fixture("unknown.rng")

        assert codes(schema).count("relaxng-unknown-attribute") == 3
        assert codes(schema).count("relaxng-unknown-element") == 1
        attributes = {
            finding.location.attribute
            for finding in schema.findings
            if finding.code == "relaxng-unknown-attribute"
        }
        assert attributes == {
            "unsupported",
            f"{{{RNG_NS}}}bogus",
            f"{{{RNG_NS}}}name",
        }
        unknown = [node for node in find_all(schema, FullUnknown) if isinstance(node, FullUnknown)]
        assert len(unknown) == 1
        assert unknown[0].tag == f"{{{RNG_NS}}}bogus"
        assert all(finding.severity == "error" for finding in schema.findings)
        assert all(isinstance(finding, SchemaFinding) for finding in schema.findings)

    def test_missing_required_attributes_are_reported(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start>
                <group>
                  <ref/>
                  <parentRef/>
                  <externalRef/>
                  <data/>
                  <param>x</param>
                </group>
              </start>
              <define><empty/></define>
              <include/>
            </grammar>
            """
        )

        missing = [
            finding for finding in schema.findings if finding.code == "relaxng-missing-attribute"
        ]
        assert len(missing) == 7
        assert {finding.location.attribute for finding in missing} == {
            "name",
            "href",
            "type",
        }
        data = find_one(schema, FullData)
        assert isinstance(data, FullData)
        assert data.type_name is None

    def test_element_and_attribute_without_a_name_class_are_reported(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}">
              <empty/>
            </element>
            """
        )
        attribute_schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute><empty/></attribute>
            </element>
            """
        )

        element = schema.start
        assert isinstance(element, FullElement)
        assert element.name_class is None
        assert codes(schema) == ["relaxng-missing-name-class"]

        attribute = find_one(attribute_schema, FullAttribute)
        assert isinstance(attribute, FullAttribute)
        assert attribute.name_class is None
        assert "relaxng-missing-name-class" in codes(attribute_schema)

    def test_name_class_except_forms_preserve_ns_and_invalid_children(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}">
              <anyName>
                <name>ignored</name>
                <except ns="urn:except" unsupported="1">
                  <except><name>inner</name></except>
                  <name>kept</name>
                </except>
              </anyName>
              <empty/>
            </element>
            """
        )

        element = schema.start
        assert isinstance(element, FullElement)
        assert isinstance(element.name_class, FullAnyName)
        except_class = element.name_class.except_class
        assert isinstance(except_class, FullNameExcept)
        assert len(except_class.alternatives) == 1
        kept = except_class.alternatives[0]
        assert isinstance(kept, FullName)
        assert (kept.uri, kept.local) == ("urn:except", "kept")
        assert "relaxng-unknown-attribute" in codes(schema)

    def test_unknown_name_class_element_is_a_finding(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}">
              <anyName>
                <except>
                  <bogus/>
                </except>
              </anyName>
              <empty/>
            </element>
            """
        )

        assert "relaxng-unknown-element" in codes(schema)
        element = schema.start
        assert isinstance(element, FullElement)
        assert isinstance(element.name_class, FullAnyName)
        assert isinstance(element.name_class.except_class, FullNameExcept)
        assert element.name_class.except_class.alternatives == ()

    def test_finding_locations_are_exact(self):
        schema = read_fixture("unknown.rng")
        unknown = next(
            finding for finding in schema.findings if finding.code == "relaxng-unknown-element"
        )

        assert unknown.location.path[-1] == PathStep(f"{{{RNG_NS}}}bogus", 1)
        assert unknown.location.source == schema.resource.canonical_id
        attribute = next(
            finding
            for finding in schema.findings
            if finding.code == "relaxng-unknown-attribute"
            and finding.location.attribute == "unsupported"
        )
        assert attribute.location.path[-1] == PathStep(f"{{{RNG_NS}}}element", 1)

    def test_findings_are_deterministic(self):
        first = read_fixture("unknown.rng")
        second = read_fixture("unknown.rng")

        assert [
            (finding.code, finding.message, finding.location) for finding in first.findings
        ] == [(finding.code, finding.message, finding.location) for finding in second.findings]

    def test_common_ns_and_datatype_library_attributes_are_not_findings(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}" ns="urn:outer" datatypeLibrary="urn:lib">
              <start>
                <group ns="" datatypeLibrary="">
                  <empty/>
                </group>
              </start>
            </grammar>
            """
        )

        assert schema.findings == ()


class TestOrigins:
    def test_every_construct_has_an_origin_in_the_loaded_resource(self, tmp_path):
        source = tmp_path / "schema.rng"
        source.write_text(
            f"""
            <grammar xmlns="{RNG_NS}" name="ignored">
              <include href="other.rng">
                <define name="override"><text/></define>
              </include>
              <start><ref name="root"/></start>
              <define name="root">
                <element name="root">
                  <attribute name="id"><data type="token"><param name="p">v</param></data></attribute>
                  <oneOrMore><element><name>child</name><empty/></element></oneOrMore>
                </element>
              </define>
            </grammar>
            """,
            encoding="utf-8",
        )
        resource = ResourceLoader().load(source)
        schema = read_full_syntax(resource)
        expected = str(source.resolve())

        nodes = list(walk(schema.start)) + list(schema.grammars)
        for node in nodes:
            origins = node.origins
            assert origins, f"{type(node).__name__} has no origin"
            assert all(origin.source_id == expected for origin in origins)

    def test_origin_locations_and_attribute_addresses_are_exact(self, tmp_path):
        source = tmp_path / "schema.rng"
        source.write_text(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><empty/></start>
              <define name="root"><element name="doc"><empty/></element></define>
            </grammar>
            """,
            encoding="utf-8",
        )
        resource = ResourceLoader().load(source)
        schema = read_full_syntax(resource)
        expected = str(source.resolve())
        scope = schema.start.scope

        define = scope.definitions[0]
        define_path = (
            PathStep(f"{{{RNG_NS}}}grammar", 1),
            PathStep(f"{{{RNG_NS}}}define", 1),
        )
        assert SourceOrigin(expected, IssueLocation(expected, define_path)) in define.origins
        assert (
            SourceOrigin(
                expected,
                IssueLocation(expected, define_path, attribute="name"),
            )
            in define.origins
        )

        element = define.children[0]
        assert isinstance(element, FullElement)
        element_path = (*define_path, PathStep(f"{{{RNG_NS}}}element", 1))
        assert (
            SourceOrigin(expected, IssueLocation(expected, element_path, attribute="name"))
            in element.origins
        )

    def test_parameter_name_attribute_origin(self):
        schema = read_fixture("datatypes.rng")
        resource = schema.resource
        parameter = find_one(schema, FullParam)
        assert isinstance(parameter, FullParam)
        param_path = (
            PathStep(f"{{{RNG_NS}}}grammar", 1),
            PathStep(f"{{{RNG_NS}}}start", 1),
            PathStep(f"{{{RNG_NS}}}element", 1),
            PathStep(f"{{{RNG_NS}}}group", 1),
            PathStep(f"{{{RNG_NS}}}attribute", 1),
            PathStep(f"{{{RNG_NS}}}data", 1),
            PathStep(f"{{{RNG_NS}}}param", 1),
        )

        for attribute in ("name",):
            origin = SourceOrigin(
                resource.canonical_id,
                IssueLocation(resource.canonical_id, param_path, attribute=attribute),
            )
            assert origin in parameter.origins

    def test_include_href_origin_is_recorded(self):
        schema = read_fixture("external/main.rng")
        include = schema.start.scope.includes[0]
        include_path = (
            PathStep(f"{{{RNG_NS}}}grammar", 1),
            PathStep(f"{{{RNG_NS}}}include", 1),
        )

        assert (
            SourceOrigin(
                schema.resource.canonical_id,
                IssueLocation(schema.resource.canonical_id, include_path, attribute="href"),
            )
            in include.origins
        )

    def test_loaded_resource_is_exposed_on_the_full_schema(self):
        schema = read_fixture("grammar.rng")
        assert isinstance(schema.resource, LoadedResource)
        assert schema.resource.canonical_id.endswith("grammar.rng")


class TestStructurePreservation:
    def test_empty_text_and_not_allowed_keep_unexpected_children(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <empty><element name="nested"><empty/></element></empty>
            </element>
            """
        )

        empty = find_one(schema, FullEmpty)
        assert isinstance(empty, FullEmpty)
        assert len(empty.children) == 1
        assert isinstance(empty.children[0], FullElement)
        assert not find_all(schema, FullNotAllowed)

    def test_unknown_subtrees_preserve_text_attributes_and_children(self):
        schema = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start>
                <bogus alpha="1" beta="2">text<child>inner</child></bogus>
              </start>
            </grammar>
            """
        )

        unknown = find_one(schema, FullUnknown)
        assert isinstance(unknown, FullUnknown)
        assert unknown.text == "text"
        assert unknown.attributes == (("alpha", "1"), ("beta", "2"))
        assert len(unknown.children) == 1
        assert unknown.children[0].text == "inner"

    def test_choice_group_interleave_and_repetitions_are_preserved(self):
        schema = read_schema(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <choice>
                <group>
                  <optional><text/></optional>
                  <zeroOrMore><text/></zeroOrMore>
                </group>
                <interleave>
                  <oneOrMore><text/></oneOrMore>
                  <list><text/></list>
                </interleave>
              </choice>
            </element>
            """
        )

        choice = find_one(schema, FullChoice)
        assert isinstance(choice, FullChoice)
        assert len(choice.children) == 2
        assert isinstance(choice.children[0], FullGroup)
        assert isinstance(choice.children[1], FullInterleave)
        assert find_all(schema, FullText)
        assert find_all(schema, FullDiv) == []
