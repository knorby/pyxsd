"""Ordered RELAX NG simplification into the local pattern graph.

The simplifier follows the specification's rule order.  Annotations,
whitespace-only schema text, inherited datatype and namespace context, name
classes, ``div`` grouping, child counts, ``mixed``/``optional``/``zeroOrMore``,
and the ``notAllowed``/``empty`` reductions are local.  Grammar scopes,
definitions, references, and ``combine`` stay unresolved for composition.
"""

from __future__ import annotations

import io

import pytest

from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeLibraryRegistry,
    DatatypeParameter,
    DatatypeParameterError,
    UnsupportedDatatypeError,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NsName
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
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
    PatternPool,
    Text,
    Value,
    choice,
    group,
    one_or_more,
)
from pyxsd.relaxng.resources import ResourceLoader
from pyxsd.relaxng.simplify import (
    BundleDefine,
    BundleInclude,
    BundleScope,
    BundleStart,
    GrammarBundle,
    LocalGrammar,
    Simplifier,
    UnresolvedReference,
    _location,
    simplify_schema,
)
from pyxsd.relaxng.syntax import FullNameClass, read_full_syntax
from pyxsd.validation import ValidationReport

RNG_NS = "http://relaxng.org/ns/structure/1.0"
XMLNS_NS = "http://www.w3.org/2000/xmlns"
TAG = f"{{{RNG_NS}}}"


def read_schema(xml: str):
    return read_full_syntax(ResourceLoader().load(io.StringIO(xml)))


def run_simplify(
    xml: str,
    *,
    report: ValidationReport | None = None,
    pool: PatternPool | None = None,
    libraries: DatatypeLibraryRegistry | None = None,
) -> tuple[GrammarBundle, ValidationReport, PatternPool]:
    report = report if report is not None else ValidationReport()
    pool = pool if pool is not None else PatternPool()
    libraries = libraries if libraries is not None else DatatypeLibraryRegistry()
    schema = read_schema(xml)
    bundle = Simplifier(report, pool, libraries).simplify(schema)
    return bundle, report, pool


def simplify_pattern(xml: str) -> object:
    return run_simplify(xml)[0].start


def codes(report: ValidationReport) -> list[str]:
    return [issue.code for issue in report.errors]


def element(name: str, content: object | None = None, uri: str | None = None) -> Element:
    return Element(Name(uri, name), Empty() if content is None else content)


def attribute(name: str, content: object | None = None, uri: str | None = None) -> Attribute:
    return Attribute(Name(uri, name), Text() if content is None else content)


def schema(body: str) -> str:
    return f'<element xmlns="{RNG_NS}" name="root">{body}</element>'


def datatype(
    library: str, type_name: str, parameters: tuple[DatatypeParameter, ...] = ()
) -> object:
    return DatatypeLibraryRegistry().create(
        (library, type_name), parameters, DatatypeContext(None, {})
    )


class RejectingFactory(DatatypeFactory):
    """A custom library whose every type is rejected at compilation."""

    def create(self, type_name, parameters, context):
        raise DatatypeParameterError("this datatype library rejects every type")


class TestConvenience:
    def test_simplify_schema_can_run_without_explicit_dependencies(self):
        bundle = simplify_schema(read_schema(schema("<empty/>")))

        assert bundle.start == element("root")
        assert bundle.scopes == ()
        assert bundle.root_scope is None

    def test_a_simplifier_can_be_reused_between_schemas(self):
        report = ValidationReport()
        simplifier = Simplifier(report, PatternPool(), DatatypeLibraryRegistry())
        grammar = read_schema(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><empty/></start>
            </grammar>
            """
        )
        bare = read_schema(schema("<empty/>"))

        first = simplifier.simplify(grammar)
        second = simplifier.simplify(bare)

        assert len(first.scopes) == 1
        assert second.scopes == ()
        assert isinstance(second.start, Element)


class TestAnnotations:
    def test_foreign_elements_and_attributes_do_not_affect_semantics(self):
        plain, _, _ = run_simplify(schema("<empty/>"))
        annotated, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root"
                     xmlns:a="urn:example:annotation" a:note="ignored">
              <a:documentation>
                <element name="fake"><text/></element>
              </a:documentation>
              <empty/>
            </element>
            """
        )

        assert annotated.start == plain.start
        assert report.errors == []

    def test_annotations_inside_value_do_not_change_the_lexical_value(self):
        annotated = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <value xmlns:a="urn:example:annotation" a:note="ignored"> 42 </value>
            </element>
            """
        )

        assert annotated == element("root", Value(datatype(BUILTIN_LIBRARY, "token"), " 42 "))

    def test_annotations_inside_name_classes_do_not_change_the_name(self):
        annotated = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element xmlns:a="urn:example:annotation">
                <name a:note="ignored">child</name>
                <empty/>
              </element>
            </element>
            """
        )

        assert annotated == element("root", element("child"))


class TestWhitespaceAndDatatypes:
    def test_value_lexical_whitespace_is_preserved(self):
        pattern = simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><value> 42 </value></element>'
        )

        assert pattern == element("root", Value(datatype(BUILTIN_LIBRARY, "token"), " 42 "))

    def test_type_and_combine_values_are_trimmed(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start combine=" choice "><ref name="r"/></start>
              <define name="r" combine=" interleave ">
                <element name="root"><empty/></element>
              </define>
            </grammar>
            """
        )

        scope = bundle.root_scope
        assert scope is not None
        assert report.errors == []
        assert scope.starts[0].combine == "choice"
        assert scope.definitions[0].combine == "interleave"

    def test_datatype_parameter_content_whitespace_is_preserved(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type=" int " datatypeLibrary="{XSD_LIBRARY}">
                <param name="pattern"> 1 </param>
              </data>
            </element>
            """
        )

        expected = Data(datatype(XSD_LIBRARY, "int", (DatatypeParameter("pattern", " 1 "),)))
        assert pattern == element("root", expected)

    def test_inherited_datatype_library_is_retained(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root" datatypeLibrary="{XSD_LIBRARY}">
              <data type="integer"/>
            </element>
            """
        )

        assert pattern == element("root", Data(datatype(XSD_LIBRARY, "integer")))

    def test_untyped_value_defaults_to_the_builtin_token_type(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root" datatypeLibrary="{XSD_LIBRARY}">
              <value>7</value>
            </element>
            """
        )

        assert pattern == element("root", Value(datatype(BUILTIN_LIBRARY, "token"), "7"))

    def test_typed_value_uses_the_inherited_datatype_library(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root" datatypeLibrary="{XSD_LIBRARY}">
              <value type="integer">7</value>
            </element>
            """
        )

        assert pattern == element("root", Value(datatype(XSD_LIBRARY, "integer"), "7"))

    def test_data_without_a_type_is_a_schema_error(self):
        bundle, report, _ = run_simplify(f'<element xmlns="{RNG_NS}" name="root"><data/></element>')

        assert bundle.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-missing-datatype"]

    def test_value_with_a_blank_type_is_a_schema_error(self):
        bundle, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><value type="  ">7</value></element>'
        )

        assert bundle.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-missing-datatype"]

    def test_custom_value_type_rejection_is_a_schema_error(self):
        libraries = DatatypeLibraryRegistry()
        libraries.register(
            "urn:example:rejecting",
            DatatypeLibraryRegistration(RejectingFactory(), "rejecting/1"),
        )
        bundle, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <value type="item" datatypeLibrary="urn:example:rejecting">x</value>
            </element>
            """,
            libraries=libraries,
        )

        assert bundle.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-invalid-datatype"]

    def test_invalid_datatype_parameters_are_schema_errors(self):
        bundle, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <param name="minInclusive">not-a-number</param>
              </data>
            </element>
            """
        )

        assert bundle.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-invalid-datatype"]
        assert all(issue.phase == "schema" for issue in report.errors)

    def test_unknown_datatypes_are_capability_failures(self):
        with pytest.raises(UnsupportedDatatypeError):
            run_simplify(
                f'<element xmlns="{RNG_NS}" name="root"><data type="noSuchType"/></element>'
            )

    def test_parameter_without_a_name_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <param>1</param>
              </data>
            </element>
            """
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-invalid-parameter"]

    def test_parameter_with_an_empty_name_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <param name="">1</param>
              </data>
            </element>
            """
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-invalid-parameter"]

    def test_padded_parameter_name_is_trimmed(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <param name=" pattern "> 1 </param>
              </data>
            </element>
            """
        )

        expected = Data(datatype(XSD_LIBRARY, "integer", (DatatypeParameter("pattern", " 1 "),)))
        assert pattern.start == element("root", expected)
        assert report.errors == []

    def test_whitespace_only_parameter_name_is_missing(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <param name="   ">1</param>
              </data>
            </element>
            """
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-invalid-parameter"]

    def test_parameter_element_children_are_reported(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="string" datatypeLibrary="{XSD_LIBRARY}">
                <param name="pattern"><element name="bad"><empty/></element></param>
              </data>
            </element>
            """
        )

        assert isinstance(pattern.start, Element)
        assert isinstance(pattern.start.pattern, Data)
        assert codes(report) == ["relaxng-unexpected-pattern"]

    def test_prefixed_names_resolve_through_the_namespace_frame(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" xmlns:p="urn:example:p" name="p:root">
              <empty/>
            </element>
            """
        )

        assert pattern == element("root", None, uri="urn:example:p")

    def test_element_ns_is_inherited_but_attribute_names_are_not(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" ns="urn:example:outer" name="root">
              <element name="child"><empty/></element>
              <attribute name="plain"/>
              <attribute ns="urn:example:attribute" name="qualified"/>
            </element>
            """
        )

        expected = element(
            "root",
            group(
                group(element("child", uri="urn:example:outer"), attribute("plain")),
                attribute("qualified", uri="urn:example:attribute"),
            ),
            uri="urn:example:outer",
        )
        assert pattern == expected

    def test_value_qname_context_uses_the_relax_ng_ns_not_the_xml_default(self):
        bundle, _, _ = run_simplify(
            f"""
            <rng:element xmlns:rng="{RNG_NS}" xmlns="urn:example:xml-default"
                         xmlns:p="urn:example:p" ns="urn:example:rng" name="root">
              <rng:value type="QName" datatypeLibrary="{XSD_LIBRARY}">p:item</rng:value>
            </rng:element>
            """
        )

        assert isinstance(bundle.start, Element)
        value = bundle.start.pattern
        assert isinstance(value, Value)
        assert value.lexical == "p:item"
        bindings = dict(value.datatype.context.namespaces)
        assert bindings[""] == "urn:example:rng"
        assert bindings["p"] == "urn:example:p"

    def test_value_with_empty_ns_removes_the_default_namespace(self):
        pattern = simplify_pattern(
            f"""
            <rng:element xmlns:rng="{RNG_NS}" xmlns="urn:example:xml-default"
                         ns="" name="root">
              <rng:value type="QName" datatypeLibrary="{XSD_LIBRARY}">item</rng:value>
            </rng:element>
            """
        )

        assert isinstance(pattern, Element)
        assert isinstance(pattern.pattern, Value)
        assert "" not in pattern.pattern.datatype.context.namespaces

    def test_data_context_preserves_an_inner_xmlns_undeclaration(self):
        pattern = simplify_pattern(
            f"""
            <rng:element xmlns:rng="{RNG_NS}" xmlns="urn:example:outer" name="root">
              <rng:data xmlns="" type="QName" datatypeLibrary="{XSD_LIBRARY}"/>
            </rng:element>
            """
        )

        assert isinstance(pattern, Element)
        assert isinstance(pattern.pattern, Data)
        bindings = dict(pattern.pattern.datatype.context.namespaces)
        assert bindings[""] == ""

    def test_data_context_retains_the_schema_base_uri(self):
        schema_node = read_schema(
            f'<element xmlns="{RNG_NS}" name="root"><data type="string"/></element>'
        )
        report = ValidationReport()
        bundle = Simplifier(report, PatternPool(), DatatypeLibraryRegistry()).simplify(schema_node)

        assert isinstance(bundle.start, Element)
        assert isinstance(bundle.start.pattern, Data)
        assert bundle.start.pattern.datatype.context.base_uri == schema_node.resource.canonical_id


class TestNameClasses:
    def test_name_class_choice_is_flattened_deduplicated_and_ordered(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <choice>
                  <choice><name>b</name><name>a</name></choice>
                  <name>a</name>
                  <name>c</name>
                </choice>
                <empty/>
              </element>
            </element>
            """
        )

        assert pattern == element(
            "root",
            Element(
                NameChoice((Name(None, "a"), Name(None, "b"), Name(None, "c"))),
                Empty(),
            ),
        )

    def test_single_alternative_name_choice_collapses(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element><choice><name>child</name></choice><empty/></element>
            </element>
            """
        )

        assert pattern == element("root", element("child"))

    def test_any_name_except_name_class(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <anyName><except><name>skip</name></except></anyName>
                <empty/>
              </element>
            </element>
            """
        )

        assert pattern == element(
            "root", Element(ExceptName(AnyName(), Name(None, "skip")), Empty())
        )

    def test_ns_name_except_name_class(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <nsName ns="urn:example:x">
                  <except><name ns="urn:example:y">skip</name></except>
                </nsName>
                <empty/>
              </element>
            </element>
            """
        )

        assert pattern == element(
            "root",
            Element(
                ExceptName(NsName("urn:example:x"), Name("urn:example:y", "skip")),
                Empty(),
            ),
        )

    def test_any_name_except_any_name_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <anyName><except><anyName/></except></anyName>
                <empty/>
              </element>
            </element>
            """
        )

        assert isinstance(pattern.start, Element)
        assert isinstance(pattern.start.pattern, Element)
        assert pattern.start.pattern.name_class == ExceptName(AnyName(), AnyName())
        assert codes(report) == ["relaxng-any-name-except"]

    def test_ns_name_except_ns_name_is_a_schema_error(self):
        _, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <nsName ns="urn:example:x">
                  <except><nsName ns="urn:example:y"/></except>
                </nsName>
                <empty/>
              </element>
            </element>
            """
        )

        assert codes(report) == ["relaxng-ns-name-except"]

    def test_empty_name_class_choice_is_reported_and_dropped_from_an_except(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <anyName><except><choice/></except></anyName>
                <empty/>
              </element>
            </element>
            """
        )

        assert isinstance(pattern.start, Element)
        assert isinstance(pattern.start.pattern, Element)
        assert pattern.start.pattern.name_class == AnyName()
        assert codes(report) == ["relaxng-empty-name-class"]

    def test_attribute_name_xmlns_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><attribute name="xmlns"/></element>'
        )

        assert pattern.start == element("root", attribute("xmlns"))
        assert codes(report) == ["relaxng-attribute-name-xmlns"]

    def test_attribute_namespace_wildcard_is_a_schema_error(self):
        _, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute><nsName ns="{XMLNS_NS}"/></attribute>
            </element>
            """
        )

        assert codes(report) == ["relaxng-attribute-name-namespace"]

    def test_attribute_name_in_the_xmlns_namespace_is_a_schema_error(self):
        _, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute ns="{XMLNS_NS}" name="foo"/>
            </element>
            """
        )

        assert codes(report) == ["relaxng-attribute-name-namespace"]

    def test_attribute_any_name_is_accepted(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><attribute><anyName/></attribute></element>'
        )

        assert pattern.start == element("root", Attribute(AnyName(), Text()))
        assert report.errors == []

    def test_attribute_name_choice_is_checked(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute>
                <choice><name>a</name><name>b</name></choice>
              </attribute>
            </element>
            """
        )

        assert pattern.start == element(
            "root",
            Attribute(NameChoice((Name(None, "a"), Name(None, "b"))), Text()),
        )
        assert report.errors == []

    def test_attribute_except_name_class_is_checked(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute>
                <anyName><except><name>skip</name></except></anyName>
              </attribute>
            </element>
            """
        )

        assert pattern.start == element(
            "root", Attribute(ExceptName(AnyName(), Name(None, "skip")), Text())
        )
        assert report.errors == []

    def test_nested_any_name_except_is_reported(self):
        _, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element>
                <anyName>
                  <except>
                    <nsName ns="urn:example:x">
                      <except><anyName/></except>
                    </nsName>
                  </except>
                </anyName>
                <empty/>
              </element>
            </element>
            """
        )

        assert "relaxng-any-name-except" in codes(report)

    def test_an_unknown_name_class_subclass_is_reported_as_empty(self):
        class MysteryNameClass(FullNameClass):
            origins = OriginTable()

        report = ValidationReport()
        simplifier = Simplifier(report, PatternPool(), DatatypeLibraryRegistry())

        assert simplifier._name_class_node(MysteryNameClass(), attribute=False) == NameChoice(())
        assert codes(report) == ["relaxng-empty-name-class"]

    def test_element_named_xmlns_without_a_namespace_is_not_an_attribute_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="xmlns"><empty/></element>'
        )

        assert pattern.start == element("xmlns")
        assert report.errors == []

    def test_missing_element_name_class_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><element><empty/></element></element>'
        )

        expected = Element(NameChoice(()), Empty())
        assert pattern.start == element("root", expected)
        assert codes(report) == ["relaxng-missing-name-class"]

    def test_missing_attribute_name_class_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><attribute>text?</attribute></element>'
        )

        expected = Attribute(NameChoice(()), Text())
        assert pattern.start == element("root", expected)
        assert codes(report) == ["relaxng-missing-name-class"]


class TestMixedOptionalAndRepetition:
    def test_mixed_becomes_an_interleave_with_text(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <mixed><element name="child"><empty/></element></mixed>
            </element>
            """
        )

        assert pattern == element("root", Interleave(element("child"), Text()))

    def test_mixed_groups_multiple_children_before_adding_text(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <mixed>
                <element name="a"><empty/></element>
                <element name="b"><empty/></element>
              </mixed>
            </element>
            """
        )

        assert pattern == element("root", Interleave(Group(element("a"), element("b")), Text()))

    def test_mixed_empty_content_becomes_text(self):
        pattern = simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><mixed><empty/></mixed></element>'
        )

        assert pattern == element("root", Text())

    def test_optional_becomes_a_choice_with_empty(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <optional><element name="child"><empty/></element></optional>
            </element>
            """
        )

        assert pattern == element("root", choice(element("child"), Empty()))

    def test_optional_empty_and_not_allowed_reduce_to_empty(self):
        assert simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><optional><empty/></optional></element>'
        ) == element("root")
        assert simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><optional><notAllowed/></optional></element>'
        ) == element("root")

    def test_zero_or_more_becomes_a_choice_of_one_or_more_and_empty(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <zeroOrMore><element name="child"><empty/></element></zeroOrMore>
            </element>
            """
        )

        assert pattern == element("root", choice(one_or_more(element("child")), Empty()))

    def test_zero_or_more_of_empty_and_not_allowed_reduce_to_empty(self):
        assert simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><zeroOrMore><empty/></zeroOrMore></element>'
        ) == element("root")
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <zeroOrMore><notAllowed/></zeroOrMore>
            </element>
            """
        ) == element("root")

    def test_one_or_more_reductions(self):
        assert simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><oneOrMore><empty/></oneOrMore></element>'
        ) == element("root")
        assert simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><oneOrMore><notAllowed/></oneOrMore></element>'
        ) == element("root", NotAllowed())


class TestDivAndChildNormalization:
    def test_a_pattern_div_flattens_into_a_group(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <div>
                <element name="a"><empty/></element>
                <element name="b"><empty/></element>
              </div>
            </element>
            """
        )

        assert pattern == element("root", Group(element("a"), element("b")))

    def test_nested_divs_flatten_in_document_order(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <group>
                <element name="a"><empty/></element>
                <div>
                  <div>
                    <element name="b"><empty/></element>
                    <div><element name="c"><empty/></element></div>
                  </div>
                </div>
              </group>
            </element>
            """
        )

        expected = Group(Group(element("a"), element("b")), element("c"))
        assert expected == group(group(element("a"), element("b")), element("c"))
        assert pattern == element("root", expected)

    def test_grammar_divs_flatten_their_components(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <div>
                <start><ref name="a"/></start>
                <div>
                  <define name="a"><element name="a"><empty/></element></define>
                  <define name="b"><element name="b"><empty/></element></define>
                </div>
              </div>
            </grammar>
            """
        )

        scope = bundle.root_scope
        assert scope is not None
        assert report.errors == []
        assert len(scope.starts) == 1
        assert [define.name for define in scope.definitions] == ["a", "b"]

    def test_element_content_wraps_multiple_patterns_in_a_group(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element name="a"><empty/></element>
              <element name="b"><empty/></element>
              <element name="c"><empty/></element>
            </element>
            """
        )

        expected = Group(Group(element("a"), element("b")), element("c"))
        assert pattern == element("root", expected)

    def test_div_origins_attach_to_the_group_they_form(self):
        bundle, _, pool = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <group>
                <div>
                  <element name="a"><empty/></element>
                  <element name="b"><empty/></element>
                </div>
              </group>
            </element>
            """
        )

        assert isinstance(bundle.start, Element)
        grouped = bundle.start.pattern
        assert isinstance(grouped, Group)
        paths = [
            tuple(step.name for step in origin.location.path)
            for origin in pool.origins(grouped)
            if origin.location is not None
        ]
        assert any(TAG + "div" in path for path in paths)

    def test_single_child_group_origins_attach_to_the_surviving_child(self):
        bundle, _, pool = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <group><element name="a"><empty/></element></group>
            </element>
            """
        )

        assert isinstance(bundle.start, Element)
        child = bundle.start.pattern
        assert isinstance(child, Element)
        paths = [
            tuple(step.name for step in origin.location.path)
            for origin in pool.origins(child)
            if origin.location is not None
        ]
        assert any(TAG + "group" in path for path in paths)

    def test_attribute_without_content_gets_text(self):
        pattern = simplify_pattern(
            f'<element xmlns="{RNG_NS}" name="root"><attribute name="a"/></element>'
        )

        assert pattern == element("root", attribute("a"))

    def test_attribute_with_multiple_patterns_is_a_schema_error(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute name="a">
                <text/>
                <empty/>
              </attribute>
            </element>
            """
        )

        assert pattern.start == element("root", Attribute(Name(None, "a"), Text()))
        assert codes(report) == ["relaxng-invalid-attribute-content"]

    def test_start_with_multiple_patterns_is_a_schema_error(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start>
                <element name="a"><empty/></element>
                <element name="b"><empty/></element>
              </start>
            </grammar>
            """
        )

        assert bundle.root_scope is not None
        assert bundle.root_scope.starts[0].pattern == Group(element("a"), element("b"))
        assert codes(report) == ["relaxng-invalid-start-content"]

    def test_start_without_a_pattern_is_a_schema_error(self):
        bundle, report, _ = run_simplify(f'<grammar xmlns="{RNG_NS}"><start/></grammar>')

        assert bundle.root_scope is not None
        assert bundle.root_scope.starts[0].pattern == NotAllowed()
        assert codes(report) == ["relaxng-invalid-start-content"]

    def test_define_with_multiple_patterns_is_grouped(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name="r"/></start>
              <define name="r">
                <element name="a"><empty/></element>
                <element name="b"><empty/></element>
              </define>
            </grammar>
            """
        )

        assert bundle.root_scope is not None
        assert bundle.root_scope.definitions[0].pattern == Group(element("a"), element("b"))
        assert report.errors == []

    def test_define_without_a_pattern_is_a_schema_error(self):
        bundle, report, _ = run_simplify(
            f'<grammar xmlns="{RNG_NS}"><start><empty/></start><define name="r"/></grammar>'
        )

        assert bundle.root_scope is not None
        assert bundle.root_scope.definitions[0].pattern == NotAllowed()
        assert codes(report) == ["relaxng-invalid-define-content"]

    def test_choice_with_one_child_is_replaced_by_the_child(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <choice><element name="a"><empty/></element></choice>
            </element>
            """
        )

        assert pattern == element("root", element("a"))

    def test_choice_with_many_children_folds_left(self):
        pattern = simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <choice>
                <element name="a"><empty/></element>
                <element name="b"><empty/></element>
                <element name="c"><empty/></element>
              </choice>
            </element>
            """
        )

        assert pattern == element(
            "root",
            choice(choice(element("a"), element("b")), element("c")),
        )

    def test_group_without_children_is_empty_with_an_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><group/></element>'
        )

        assert pattern.start == element("root")
        assert codes(report) == ["relaxng-empty-pattern-content"]

    def test_choice_without_children_is_not_allowed_with_an_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><choice/></element>'
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-empty-pattern-content"]

    def test_element_without_content_is_not_allowed_with_an_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><element name="child"/></element>'
        )

        assert pattern.start == element("root", Element(Name(None, "child"), NotAllowed()))
        assert codes(report) == ["relaxng-empty-pattern-content"]

    def test_list_without_content_is_not_allowed_with_an_error(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><list/></element>'
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-empty-pattern-content"]

    def test_list_with_content_keeps_the_list_pattern(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><list><text/></list></element>'
        )

        assert pattern.start == element("root", List(Text()))
        assert report.errors == []

    def test_reference_children_are_reported_and_dropped(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <ref name="x"><element name="ignored"><empty/></element></ref>
            </element>
            """
        )

        assert isinstance(pattern.start, Element)
        assert isinstance(pattern.start.pattern, UnresolvedReference)
        assert pattern.start.pattern.name == "x"
        assert codes(report) == ["relaxng-unexpected-pattern"]

    def test_empty_non_pattern_children_are_reported(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><text><empty/></text></element>'
        )

        assert pattern.start == element("root", Text())
        assert codes(report) == ["relaxng-unexpected-pattern"]

    def test_value_children_are_reported(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><value>1<empty/></value></element>'
        )

        assert pattern.start == element("root", Value(datatype(BUILTIN_LIBRARY, "token"), "1"))
        assert codes(report) == ["relaxng-unexpected-pattern"]

    def test_param_outside_data_is_reported(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><param name="p">v</param></element>'
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-unexpected-pattern"]

    def test_unknown_pattern_is_reported_as_not_allowed(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><unknown/></element>'
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-unsupported-pattern"]

    def test_include_in_pattern_position_is_reported(self):
        pattern, report, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><include href="part.rng"/></element>'
        )

        assert pattern.start == element("root", NotAllowed())
        assert codes(report) == ["relaxng-unexpected-pattern"]

    def test_top_level_div_with_one_child_is_replaced(self):
        bundle, report, _ = run_simplify(
            f"""
            <div xmlns="{RNG_NS}">
              <element name="root"><empty/></element>
            </div>
            """
        )

        assert bundle.start == element("root")
        assert report.errors == []

    def test_top_level_div_with_multiple_children_is_a_schema_error(self):
        bundle, report, _ = run_simplify(
            f"""
            <div xmlns="{RNG_NS}">
              <element name="a"><empty/></element>
              <element name="b"><empty/></element>
            </div>
            """
        )

        assert bundle.start == Group(element("a"), element("b"))
        assert codes(report) == ["relaxng-invalid-div-content"]

    def test_top_level_empty_div_is_a_schema_error(self):
        bundle, report, _ = run_simplify(f'<div xmlns="{RNG_NS}"/>')

        assert bundle.start == NotAllowed()
        assert codes(report) == ["relaxng-empty-pattern-content"]

    def test_grammar_level_extra_pattern_is_a_schema_error(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><empty/></start>
              <element name="stray"><empty/></element>
            </grammar>
            """
        )

        assert bundle.root_scope is not None
        assert bundle.root_scope.children == bundle.root_scope.starts
        assert codes(report) == ["relaxng-invalid-grammar-content"]

    def test_include_content_rejects_nested_includes_and_patterns(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><empty/></start>
              <include href="part.rng">
                <include href="nested.rng"/>
                <element name="stray"><empty/></element>
                <define name="override"><element name="x"><empty/></element></define>
              </include>
            </grammar>
            """
        )

        assert bundle.root_scope is not None
        include = bundle.root_scope.includes[0]
        assert [component.name for component in include.components] == ["override"]
        assert codes(report) == [
            "relaxng-invalid-include-content",
            "relaxng-invalid-include-content",
        ]

    def test_data_except_with_multiple_patterns_choices_them(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <except>
                  <value type="integer">1</value>
                  <value type="integer">2</value>
                </except>
              </data>
            </element>
            """
        )

        expected = DataExcept(
            datatype(XSD_LIBRARY, "integer"),
            choice(
                Value(datatype(XSD_LIBRARY, "integer"), "1"),
                Value(datatype(XSD_LIBRARY, "integer"), "2"),
            ),
        )
        assert pattern.start == element("root", expected)
        assert report.errors == []

    def test_duplicate_data_except_is_reported(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <except><value type="integer">1</value></except>
                <except><value type="integer">2</value></except>
              </data>
            </element>
            """
        )

        expected = DataExcept(
            datatype(XSD_LIBRARY, "integer"),
            choice(
                Value(datatype(XSD_LIBRARY, "integer"), "1"),
                Value(datatype(XSD_LIBRARY, "integer"), "2"),
            ),
        )
        assert pattern.start == element("root", expected)
        assert codes(report) == ["relaxng-duplicate-except"]

    def test_empty_data_except_is_reported_and_removed(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}"><except/></data>
            </element>
            """
        )

        assert pattern.start == element("root", Data(datatype(XSD_LIBRARY, "integer")))
        assert codes(report) == ["relaxng-invalid-except-content"]

    def test_unexpected_child_inside_data_is_reported(self):
        pattern, report, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <element name="stray"><empty/></element>
              </data>
            </element>
            """
        )

        assert pattern.start == element("root", Data(datatype(XSD_LIBRARY, "integer")))
        assert codes(report) == ["relaxng-unexpected-pattern"]


class TestGrammarBundle:
    def test_grammar_bundle_preserves_scopes_definitions_and_references(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start combine="choice"><ref name="root"/></start>
              <define name="root">
                <element name="root"><empty/></element>
              </define>
            </grammar>
            """
        )

        assert report.errors == []
        assert isinstance(bundle.start, LocalGrammar)
        scope = bundle.root_scope
        assert scope is bundle.start.scope
        assert isinstance(scope, BundleScope)
        assert scope.parent is None
        assert scope.key == 0
        assert scope.origins
        assert repr(scope) == "BundleScope(key=0, starts=1, definitions=1, includes=0)"
        assert repr(bundle.start) == "LocalGrammar(scope=0)"
        assert [start.combine for start in scope.starts] == ["choice"]
        assert [define.name for define in scope.definitions] == ["root"]
        start_pattern = scope.starts[0].pattern
        assert isinstance(start_pattern, UnresolvedReference)
        assert start_pattern.kind == "ref"
        assert start_pattern.name == "root"
        assert start_pattern.scope is scope
        assert start_pattern.href is None
        assert (
            repr(start_pattern)
            == "UnresolvedReference(kind='ref', name='root', href=None, scope=0)"
        )
        assert scope.definitions[0].pattern == element("root")

    def test_padded_ref_and_define_names_are_trimmed(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name=" root "/></start>
              <define name=" root ">
                <element name="root"><empty/></element>
              </define>
            </grammar>
            """
        )

        assert report.errors == []
        scope = bundle.root_scope
        assert scope is not None
        reference = scope.starts[0].pattern
        assert isinstance(reference, UnresolvedReference)
        assert reference.name == "root"
        assert scope.definitions[0].name == "root"

    def test_padded_parent_ref_name_is_trimmed(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name="root"/></start>
              <define name="root">
                <element name="root">
                  <grammar>
                    <start><parentRef name=" shape "/></start>
                  </grammar>
                </element>
              </define>
              <define name="shape"><element name="shape"><empty/></element></define>
            </grammar>
            """
        )

        assert report.errors == []
        inner = bundle.scopes[1]
        parent_ref = inner.starts[0].pattern
        assert isinstance(parent_ref, UnresolvedReference)
        assert parent_ref.name == "shape"

    def test_blank_ref_and_define_names_are_missing(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name="   "/></start>
              <define name="   ">
                <element name="root"><empty/></element>
              </define>
            </grammar>
            """
        )

        scope = bundle.root_scope
        assert scope is not None
        reference = scope.starts[0].pattern
        assert isinstance(reference, UnresolvedReference)
        assert reference.name is None
        assert scope.definitions[0].name is None
        assert codes(report) == ["relaxng-missing-name", "relaxng-missing-name"]

    def test_spec_whitespace_case_keeps_padded_names_consistent(self):
        bundle, report, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start combine=" \t\r\nchoice ">
                <ref name=" \t\r\nx \t\r\n"/>
              </start>
              <define name="x"><ref name="y"/></define>
              <define name=" \t\r\ny \t\r\n">
                <grammar>
                  <start combine=" \t\r\ninterleave ">
                    <parentRef name=" \t\r\nz \t\r\n"/>
                  </start>
                </grammar>
              </define>
              <define name="z"><element name="z"><empty/></element></define>
            </grammar>
            """
        )

        assert report.errors == []
        outer = bundle.root_scope
        assert outer is not None
        assert outer.starts[0].combine == "choice"
        start_ref = outer.starts[0].pattern
        assert isinstance(start_ref, UnresolvedReference)
        assert start_ref.name == "x"
        assert [define.name for define in outer.definitions] == ["x", "y", "z"]
        inner = bundle.scopes[1]
        assert inner.starts[0].combine == "interleave"
        parent_ref = inner.starts[0].pattern
        assert isinstance(parent_ref, UnresolvedReference)
        assert parent_ref.name == "z"

    def test_bare_pattern_bundle_keeps_references_outside_a_scope(self):
        bundle, _, _ = run_simplify(
            f'<element xmlns="{RNG_NS}" name="root"><ref name="x"/></element>'
        )

        assert bundle.scopes == ()
        assert isinstance(bundle.start, Element)
        assert isinstance(bundle.start.pattern, UnresolvedReference)
        assert bundle.start.pattern.scope is None
        assert (
            repr(bundle.start.pattern)
            == "UnresolvedReference(kind='ref', name='x', href=None, scope=None)"
        )

    def test_nested_grammar_and_parent_ref_keep_the_parent_scope(self):
        bundle, _, _ = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name="root"/></start>
              <define name="root">
                <element name="root">
                  <grammar>
                    <start><parentRef name="shape"/></start>
                  </grammar>
                </element>
              </define>
              <define name="shape"><element name="shape"><empty/></element></define>
            </grammar>
            """
        )

        outer = bundle.root_scope
        assert outer is not None
        inner = bundle.scopes[1]
        assert inner.parent is outer
        definition = outer.definitions[0]
        assert isinstance(definition.pattern, Element)
        grammar_node = definition.pattern.pattern
        assert isinstance(grammar_node, LocalGrammar)
        assert grammar_node.scope is inner
        parent_ref = inner.starts[0].pattern
        assert isinstance(parent_ref, UnresolvedReference)
        assert parent_ref.kind == "parentRef"
        assert parent_ref.scope is inner

    def test_external_ref_preserves_href_base_and_ns(self):
        bundle, _, _ = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <externalRef href="part.rng" ns="urn:example:external"/>
            </element>
            """
        )

        assert isinstance(bundle.start, Element)
        external = bundle.start.pattern
        assert isinstance(external, UnresolvedReference)
        assert external.kind == "externalRef"
        assert external.href == "part.rng"
        assert external.base is not None
        assert external.ns_attribute == "urn:example:external"

    def test_include_overrides_are_preserved_with_their_origins(self):
        bundle, report, pool = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><empty/></start>
              <include href="part.rng">
                <start><ref name="override"/></start>
                <define name="override"><element name="x"><empty/></element></define>
              </include>
            </grammar>
            """
        )

        assert report.errors == []
        scope = bundle.root_scope
        assert scope is not None
        include = scope.includes[0]
        assert isinstance(include, BundleInclude)
        assert include.href == "part.rng"
        assert [type(component) for component in include.components] == [
            BundleStart,
            BundleDefine,
        ]
        pattern = include.components[1].pattern
        assert pattern == element("x")
        assert pool.origins(pattern)


class TestAlgebraicReductions:
    def test_not_allowed_propagates_through_containers(self):
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <attribute name="a"><notAllowed/></attribute>
            </element>
            """
        ) == element("root", NotAllowed())
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <list><notAllowed/></list>
            </element>
            """
        ) == element("root", NotAllowed())
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <interleave><notAllowed/><text/></interleave>
            </element>
            """
        ) == element("root", NotAllowed())

    def test_element_with_not_allowed_content_is_still_an_element(self):
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element name="child"><notAllowed/></element>
            </element>
            """
        ) == element("root", Element(Name(None, "child"), NotAllowed()))

    def test_empty_propagates_through_containers(self):
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <interleave><empty/><element name="a"><empty/></element></interleave>
            </element>
            """
        ) == element("root", element("a"))

    def test_not_allowed_except_is_removed_from_data(self):
        assert simplify_pattern(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <data type="integer" datatypeLibrary="{XSD_LIBRARY}">
                <except><notAllowed/></except>
              </data>
            </element>
            """
        ) == element("root", Data(datatype(XSD_LIBRARY, "integer")))


class TestOriginsAndBudgets:
    def test_element_origins_include_the_name_class(self):
        bundle, _, pool = run_simplify(
            f"""
            <element xmlns="{RNG_NS}" name="root">
              <element><name>child</name><empty/></element>
            </element>
            """
        )
        outer = bundle.start
        assert isinstance(outer, Element)
        paths = [
            tuple(step.name for step in origin.location.path)
            for origin in pool.origins(outer)
            if origin.location is not None
        ]
        assert (TAG + "element",) in paths

        nested = outer.pattern
        assert isinstance(nested, Element)
        paths = [
            tuple(step.name for step in origin.location.path)
            for origin in pool.origins(nested)
            if origin.location is not None
        ]
        assert any(TAG + "element" in path for path in paths)
        assert any(TAG + "name" in path for path in paths)

    def test_mixed_and_optional_synthesized_nodes_retain_authored_origins(self):
        xml = f"""
        <element xmlns="{RNG_NS}" name="root">
          <optional><mixed><element name="a"><empty/></element></mixed></optional>
        </element>
        """
        bundle, _, pool = run_simplify(xml)
        pattern = bundle.start
        assert isinstance(pattern, Element)
        optional_choice = pattern.pattern
        assert isinstance(optional_choice, Choice)
        mixed = next(
            part
            for part in (optional_choice.first, optional_choice.second)
            if isinstance(part, Interleave)
        )
        text = mixed.second
        assert isinstance(text, Text)

        paths = [
            tuple(step.name for step in origin.location.path)
            for origin in pool.origins(text)
            if origin.location is not None
        ]
        assert any(TAG + "mixed" in path for path in paths)

    def test_reference_origins_are_retained(self):
        bundle, _, pool = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name="root"/></start>
              <define name="root"><element name="root"><empty/></element></define>
            </grammar>
            """
        )
        scope = bundle.root_scope
        assert scope is not None
        reference = scope.starts[0].pattern
        paths = [
            tuple(step.name for step in origin.location.path)
            for origin in pool.origins(reference)
            if origin.location is not None
        ]
        assert any(TAG + "ref" in path for path in paths)

    def test_definition_patterns_retain_their_origins(self):
        bundle, _, pool = run_simplify(
            f"""
            <grammar xmlns="{RNG_NS}">
              <start><ref name="root"/></start>
              <define name="root"><element name="root"><empty/></element></define>
            </grammar>
            """
        )
        scope = bundle.root_scope
        assert scope is not None
        definition = scope.definitions[0]
        assert pool.origins(definition.pattern)

    def test_pattern_budget_exhaustion_is_not_semantic_invalidity(self):
        report = ValidationReport()
        pool = PatternPool(BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=0)))
        with pytest.raises(RelaxNGLimitError):
            run_simplify(schema("<empty/>"), report=report, pool=pool)

        assert report.errors == []


class TestReportContract:
    def test_an_origin_without_an_address_has_no_location(self):
        assert _location(OriginTable({SourceOrigin("unaddressed.rng", None)})) is None

    def test_rewrite_findings_are_schema_phase_errors(self):
        _, report, _ = run_simplify(f'<element xmlns="{RNG_NS}" name="root"><group/></element>')

        assert len(report.errors) == 1
        issue = report.errors[0]
        assert issue.code == "relaxng-empty-pattern-content"
        assert issue.phase == "schema"
        assert issue.location is not None
        assert issue.location.path[-1].name == TAG + "group"
