"""Tests for the mixin ``Extensions`` registry.

The registry is pure selection bookkeeping: each ``for_*`` method compiles
its selector to a predicate over a type element representative (ER) at
registration time, and :meth:`Extensions.mixins_for` returns the matched
mixins in precedence order (later registration wins, first element is the
highest precedence). Nothing here touches generated classes; that wiring
belongs to later mixin-extension tasks.
"""

from io import StringIO

from pyxsd.extensions import Extensions
from pyxsd.schema import Schema

XSD = "http://www.w3.org/2001/XMLSchema"
NS_A = "http://example.com/a"
NS_B = "http://example.com/b"
NS_M = "http://example.com/mixin"

SCHEMA_A = f"""<xs:schema xmlns:xs="{XSD}" xmlns:a="{NS_A}"
    targetNamespace="{NS_A}" elementFormDefault="qualified">
  <xs:simpleType name="Code">
    <xs:restriction base="xs:string">
      <xs:pattern value="[A-Z]{{2}}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="Patient">
    <xs:sequence>
      <xs:element name="name" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="Dead">
    <xs:complexContent>
      <xs:extension base="a:Patient">
        <xs:sequence>
          <xs:element name="cause" type="xs:string"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
</xs:schema>"""

SCHEMA_B = f"""<xs:schema xmlns:xs="{XSD}" targetNamespace="{NS_B}"
    elementFormDefault="qualified">
  <xs:complexType name="Record">
    <xs:sequence>
      <xs:element name="value" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>"""


def _type_ers():
    """Every type ER from both compiled schemas, keyed by local name."""
    from pyxsd import Schema

    schema_a = Schema.compile(StringIO(SCHEMA_A))
    schema_b = Schema.compile(StringIO(SCHEMA_B))
    ers = {}
    for schema in (schema_a, schema_b):
        for entries in schema.components.values():
            for er in entries:
                if type(er).__name__ in ("SimpleType", "ComplexType"):
                    ers[er.name] = er
    return ers


def _code():
    return _type_ers()["Code"]


def _patient():
    return _type_ers()["Patient"]


def _dead():
    return _type_ers()["Dead"]


def _record():
    return _type_ers()["Record"]


class TestNameSelection:
    def test_local_name_matches(self):
        class CodeMixin:
            pass

        ext = Extensions()
        ext.for_type("Code", mixin=CodeMixin)
        assert ext.mixins_for(_code()) == (CodeMixin,)
        assert ext.mixins_for(_patient()) == ()

    def test_clark_name_matches(self):
        class CodeMixin:
            pass

        ext = Extensions()
        ext.for_type(f"{{{NS_A}}}Code", mixin=CodeMixin)
        assert ext.mixins_for(_code()) == (CodeMixin,)

    def test_no_match_for_other_types(self):
        class CodeMixin:
            pass

        ext = Extensions()
        ext.for_type("Code", mixin=CodeMixin)
        assert ext.mixins_for(_patient()) == ()
        assert ext.mixins_for(_dead()) == ()
        assert ext.mixins_for(_record()) == ()


class TestNamespaceSelection:
    def test_namespace_hits_all_three_types(self):
        class NsMixin:
            pass

        ext = Extensions()
        ext.for_namespace(NS_A, mixin=NsMixin)
        for er in (_code(), _patient(), _dead()):
            assert ext.mixins_for(er) == (NsMixin,)
        assert ext.mixins_for(_record()) == ()

    def test_other_namespace_hits_its_own_types_only(self):
        class NsMixin:
            pass

        ext = Extensions()
        ext.for_namespace(NS_B, mixin=NsMixin)
        assert ext.mixins_for(_record()) == (NsMixin,)
        assert ext.mixins_for(_patient()) == ()


class TestKindSelection:
    def test_complex_type_kind_hits_derived_and_base_not_simple(self):
        class ComplexMixin:
            pass

        ext = Extensions()
        ext.for_kind("complexType", mixin=ComplexMixin)
        assert ext.mixins_for(_patient()) == (ComplexMixin,)
        assert ext.mixins_for(_dead()) == (ComplexMixin,)
        assert ext.mixins_for(_code()) == ()

    def test_simple_kind_with_derived_from(self):
        class StringMixin:
            pass

        ext = Extensions()
        ext.for_kind("simpleType", derived_from="string", mixin=StringMixin)
        assert ext.mixins_for(_code()) == (StringMixin,)
        assert ext.mixins_for(_patient()) == ()

    def test_derived_from_matches_prefixed_superclass_spelling(self):
        class PatientMixin:
            pass

        ext = Extensions()
        ext.for_kind("complexType", derived_from="Patient", mixin=PatientMixin)
        # ``Dead`` extends ``a:Patient``; the local spelling matches.
        assert ext.mixins_for(_dead()) == (PatientMixin,)
        assert ext.mixins_for(_patient()) == ()

    def test_kind_without_derived_from_ignores_superclasses(self):
        class ComplexMixin:
            pass

        ext = Extensions()
        ext.for_kind("complexType", mixin=ComplexMixin)
        # ``Patient`` itself has no superclass entries; still selected.
        assert ext.mixins_for(_patient()) == (ComplexMixin,)


class TestWhereSelection:
    def test_predicate_over_the_type_er(self):
        class LongNameMixin:
            pass

        ext = Extensions()
        ext.where(lambda er: er.name in ("Patient", "Dead"), mixin=LongNameMixin)
        assert ext.mixins_for(_patient()) == (LongNameMixin,)
        assert ext.mixins_for(_dead()) == (LongNameMixin,)
        assert ext.mixins_for(_code()) == ()


class TestDecoratorAndDirectForms:
    def test_decorator_and_direct_forms_are_equivalent(self):
        ext = Extensions()

        @ext.for_type("Code")
        class CodeMixin:
            pass

        class OtherMixin:
            pass

        ext.for_namespace(NS_A, mixin=OtherMixin)

        @ext.for_kind("complexType")
        class ComplexMixin:
            pass

        class WhereMixin:
            pass

        ext.where(lambda er: er.name == "Patient", mixin=WhereMixin)

        # Later registration outranks earlier: the namespace mixin (second
        # registration) precedes the type mixin (first).
        assert ext.mixins_for(_code()) == (OtherMixin, CodeMixin)
        assert ext.mixins_for(_dead()) == (ComplexMixin, OtherMixin)
        assert ext.mixins_for(_patient()) == (WhereMixin, ComplexMixin, OtherMixin)

    def test_decorator_returns_the_decorated_class(self):
        ext = Extensions()

        @ext.for_type("Code")
        class CodeMixin:
            marker = "kept"

        assert CodeMixin.marker == "kept"


class TestPrecedence:
    def test_two_selectors_matching_one_type_yield_the_mixin_once(self):
        class SharedMixin:
            pass

        ext = Extensions()
        ext.for_type("Code", mixin=SharedMixin)
        ext.for_namespace(NS_A, mixin=SharedMixin)
        assert ext.mixins_for(_code()) == (SharedMixin,)

    def test_two_mixins_in_registration_order_later_first(self):
        class FirstMixin:
            pass

        class SecondMixin:
            pass

        ext = Extensions()
        ext.for_type("Code", mixin=FirstMixin)
        ext.for_namespace(NS_A, mixin=SecondMixin)
        assert ext.mixins_for(_code()) == (SecondMixin, FirstMixin)

    def test_dedup_is_by_identity_not_equality(self):
        def make_mixin():
            class SameNameMixin:
                pass

            SameNameMixin.__qualname__ = "test_dedup.<locals>.SameNameMixin"
            return SameNameMixin

        mixin_one = make_mixin()
        mixin_two = make_mixin()  # same name, distinct object
        assert mixin_one is not mixin_two
        assert mixin_one.__name__ == mixin_two.__name__

        ext = Extensions()
        ext.for_type("Code", mixin=mixin_one)
        ext.for_namespace(NS_A, mixin=mixin_two)
        result = ext.mixins_for(_code())
        assert len(result) == 2
        assert set(result) == {mixin_one, mixin_two}


class TestPathEntries:
    def test_for_path_is_stored_raw_and_skipped_by_mixins_for(self):
        class PathMixin:
            pass

        ext = Extensions()
        ext.for_path("patient/contact", mixin=PathMixin)
        # Task 3 resolves path entries; until then they never match and
        # must not crash.
        assert ext.mixins_for(_code()) == ()
        assert ext.mixins_for(_patient()) == ()


class TestRegistryIsolation:
    def test_instances_are_independent(self):
        class One:
            pass

        class Two:
            pass

        ext_one = Extensions()
        ext_two = Extensions()
        ext_one.for_type("Code", mixin=One)
        ext_two.for_type("Code", mixin=Two)
        assert ext_one.mixins_for(_code()) == (One,)
        assert ext_two.mixins_for(_code()) == (Two,)


# -- compile plumbing (Task 2) ----------------------------------------

COMPILE_SCHEMA = f"""<xs:schema xmlns:xs="{XSD}" xmlns:m="{NS_M}"
    targetNamespace="{NS_M}" elementFormDefault="qualified">
  <xs:simpleType name="Code">
    <xs:restriction base="xs:string">
      <xs:pattern value="[A-Z]{{2}}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="Base">
    <xs:sequence>
      <xs:element name="name" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="Patient">
    <xs:complexContent>
      <xs:extension base="m:Base">
        <xs:sequence>
          <xs:element name="diagnosis" type="xs:string"/>
        </xs:sequence>
        <xs:attribute name="id" type="m:Code"/>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:complexType name="Inpatient">
    <xs:complexContent>
      <xs:extension base="m:Patient">
        <xs:sequence>
          <xs:element name="ward" type="xs:string"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="patient" type="m:Patient"/>
</xs:schema>"""

COMPILE_INSTANCE = f"""<m:patient xmlns:m="{NS_M}" id="AB">
  <m:name>John</m:name>
  <m:diagnosis>OK</m:diagnosis>
</m:patient>"""


class TestCompilePlumbing:
    def test_mixin_method_on_generated_class(self):
        ext = Extensions()

        @ext.for_type("Patient")
        class PatientMixin:
            def label(self):
                return "patient"

        schema = Schema.compile(StringIO(COMPILE_SCHEMA), extensions=ext)
        patient_cls = schema.classes["Patient"]
        assert patient_cls.label is PatientMixin.label
        doc = schema.parse(StringIO(COMPILE_INSTANCE))
        assert doc.root.label() == "patient"

    def test_super_reaches_xsd_base(self):
        ext = Extensions()

        @ext.for_type("Base")
        class BaseMixin:
            def kind(self):
                return "base"

        @ext.for_type("Patient")
        class PatientMixin:
            def kind(self):
                return "derived:" + super().kind()

        schema = Schema.compile(StringIO(COMPILE_SCHEMA), extensions=ext)
        doc = schema.parse(StringIO(COMPILE_INSTANCE))
        assert doc.root.kind() == "derived:base"

    def test_mixin_cannot_shadow_generated_descriptor(self):
        ext = Extensions()

        @ext.for_type("Patient")
        class PatientMixin:
            id = "MIXIN"

        schema = Schema.compile(StringIO(COMPILE_SCHEMA), extensions=ext)
        patient_cls = schema.classes["Patient"]
        # The generated class's namespace carries the attribute
        # descriptor under ``id``; the mixin's member stays buried in
        # the base it belongs to.
        assert "id" in patient_cls.__dict__
        assert patient_cls.__dict__["id"] is not PatientMixin.id
        # The descriptor still functions: parse binds and reads it.
        doc = schema.parse(StringIO(COMPILE_INSTANCE))
        assert doc.root.id == "AB"

    def test_duplicate_match_appears_once(self):
        class SharedMixin:
            pass

        ext = Extensions()
        ext.for_type("Patient", mixin=SharedMixin)
        ext.for_namespace(NS_M, mixin=SharedMixin)
        schema = Schema.compile(StringIO(COMPILE_SCHEMA), extensions=ext)
        patient_cls = schema.classes["Patient"]
        assert sum(1 for klass in patient_cls.__mro__ if klass is SharedMixin) == 1

    def test_one_class_identity(self):
        ext = Extensions()

        @ext.for_type("Patient")
        class PatientMixin:
            pass

        schema = Schema.compile(StringIO(COMPILE_SCHEMA), extensions=ext)
        patient_cls = schema.classes["Patient"]
        doc = schema.parse(StringIO(COMPILE_INSTANCE))
        assert type(doc.root) is patient_cls
        # A type derived from Patient sees the mixin through its base's
        # MRO.
        assert issubclass(schema.classes["Inpatient"], PatientMixin)

    def test_compile_without_extensions_unchanged(self):
        class PatientMixin:
            pass

        ext = Extensions()
        ext.for_type("Patient", mixin=PatientMixin)
        plain = Schema.compile(StringIO(COMPILE_SCHEMA))
        extended = Schema.compile(StringIO(COMPILE_SCHEMA), extensions=ext)
        assert plain.classes["Patient"] is not extended.classes["Patient"]
        assert not issubclass(plain.classes["Patient"], PatientMixin)
