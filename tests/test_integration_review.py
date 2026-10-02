"""Regression cases independently discovered during whole-branch review."""

import pytest

from integration_helpers import compile_schema, parse


def test_validator_identifier_cannot_be_shadowed_by_xml_field():
    pd = pytest.importorskip("pydantic")
    from pyxsd.integrations.pydantic import models

    s = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="xml_contract" type="xs:int"/><xs:sequence minOccurs="0">
    <xs:element name="x" type="xs:int"/><xs:element name="y" type="xs:int"/>
    </xs:sequence></xs:sequence></xs:complexType></xs:element>""")
    Root = models(s).model_for(element="r")
    with pytest.raises(pd.ValidationError):
        Root.model_validate({"xml_contract": 1, "x": 2})


@pytest.mark.parametrize("item", ["a b", "", " "])
def test_list_items_must_not_split_or_disappear(item):
    pd = pytest.importorskip("pydantic")
    from pyxsd.integrations.pydantic import models

    s = compile_schema(
        '<xs:element name="r"><xs:simpleType><xs:list itemType="xs:string"/></xs:simpleType></xs:element>'
    )
    with pytest.raises(pd.ValidationError):
        models(s).model_for(element="r").model_validate([item])


@pytest.mark.parametrize(
    "dtype,fixed,supplied",
    [
        ("double", "NaN", float("nan")),
        ("dateTime", "2026-01-01T00:00:00Z", "2025-12-31T19:00:00-05:00"),
        ("duration", "P1Y", "P12M"),
    ],
)
def test_fixed_values_compare_in_native_xsd_value_space(dtype, fixed, supplied):
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import project

    s = compile_schema(f'<xs:element name="r" type="xs:{dtype}" fixed="{fixed}"/>')
    lexical = "NaN" if dtype == "double" else supplied
    d = parse(s, f"<r>{lexical}</r>")
    project(ShapeSet(s).resolve("r"), d.root)
    pytest.importorskip("pydantic")
    from pyxsd.integrations.pydantic import models

    models(s).model_for(element="r").model_validate(supplied)


def test_fixed_nillable_declaration_rejects_null_and_mutated_nil():
    pd = pytest.importorskip("pydantic")
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations.pydantic import models

    s = compile_schema('<xs:element name="r" type="xs:int" nillable="true" fixed="2"/>')
    registry = models(s)
    with pytest.raises(pd.ValidationError):
        registry.model_for(element="r").model_validate(None)
    d = parse(s, "<r>2</r>")
    d.root._nil_, d.root._value_ = True, None
    with pytest.raises(IntegrationError, match="fixed"):
        registry.from_document(d)


def test_required_choice_with_empty_branch_accepts_empty_content():
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import project

    s = compile_schema("""<xs:element name="r"><xs:complexType><xs:choice>
    <xs:element name="a" type="xs:int" minOccurs="0"/><xs:element name="b" type="xs:int"/>
    </xs:choice></xs:complexType></xs:element>""")
    assert project(ShapeSet(s).resolve("r"), parse(s, "<r/>").root) == {}
    pytest.importorskip("pydantic")
    from pyxsd.integrations.pydantic import models

    models(s).model_for(element="r").model_validate({})


def test_namespace_override_does_not_reuse_stale_qname_identity():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import project

    s = compile_schema('<xs:element name="r" type="xs:QName"/>')
    d = parse(s, '<r xmlns:p="urn:old">p:a</r>')
    d.root._attribs_["xmlns:p"] = "urn:new"
    with pytest.raises(IntegrationError, match="namespace"):
        project(ShapeSet(s).resolve("r"), d.root)


def test_contextual_qname_lists_are_rejected_at_preparation():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._shape import ShapeSet

    s = compile_schema(
        '<xs:element name="r"><xs:simpleType><xs:list itemType="xs:QName"/></xs:simpleType></xs:element>'
    )
    with pytest.raises(IntegrationError, match="contextual"):
        ShapeSet(s).resolve("r")


def test_python_names_do_not_collide_with_other_xml_aliases():
    pytest.importorskip("pydantic")
    from pyxsd.integrations.pydantic import models

    s = compile_schema(
        """<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="x" type="xs:int"/><xs:element name="x" form="unqualified" type="xs:int"/>
    </xs:sequence></xs:complexType></xs:element>""",
        namespace="urn:t",
    )
    Root = models(s).model_for(element="{urn:t}r")
    names = {field.alias: name for name, field in Root.model_fields.items()}
    model = Root.model_validate({names["{urn:t}x"]: 1, names["x"]: 2})
    assert model.model_dump(by_alias=True) == {"{urn:t}x": 1, "x": 2}
