"""Semantic regressions from the whole-branch JSON conversion review."""

from __future__ import annotations

import copy
import json
import math
from decimal import Decimal
from io import StringIO

import pytest
from jsonschema import Draft202012Validator

import pyxsd
from pyxsd.data_conversion.layouts import Layout, plan_layout
from pyxsd.data_conversion.values import scalar_to_json, scalar_to_lexical
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.xsd_data_types import Double

FORMATS = ("pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml")


def compile_body(body, extra=""):
    return pyxsd.compile(
        StringIO(
            f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" {extra}>{body}</xs:schema>'
        )
    )


def assert_xml_roundtrip(schema, document, check):
    document.require_valid()
    check(document)
    for restored in (schema.parse(StringIO(document.to_string())), document.revalidate()):
        restored.require_valid()
        check(restored)


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("lexical,number", [("true", 1), ("false", 0)])
def test_native_boolean_legacy_json_and_schema(format, lexical, number):
    schema = compile_body('<xs:element name="root" type="xs:boolean"/>')
    doc = schema.parse(StringIO(f"<root>{lexical}</root>"))
    data = doc.to_data(format=format)
    expected = number if format == "pyxsd" else bool(number)
    scalar = (
        data["$"]
        if format == "pyxsd"
        else (
            data["root"]
            if format in ("pyxsd-json", "badgerfish")
            else (data[-1] if format == "jsonml" else data)
        )
    )
    assert type(scalar) is type(expected)
    assert scalar == expected
    Draft202012Validator(schema.json_schema(format=format)).validate(data)
    restored = schema.from_json(json.dumps(data), format=format)

    def check(d):
        assert int(d.root) == number

    assert_xml_roundtrip(schema, restored, check)


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("lexical", ["NaN", "INF", "-INF", "1.5"])
def test_union_numeric_member_uses_numeric_contract(format, lexical):
    schema = compile_body("""
      <xs:simpleType name="Value"><xs:union memberTypes="xs:double xs:string"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    doc = schema.parse(StringIO(f"<root>{lexical}</root>"))
    data = doc.to_data(format=format)
    expected = 1.5 if lexical == "1.5" else lexical
    scalar = (
        data["$"]
        if format == "pyxsd"
        else (
            data["root"]
            if format in ("pyxsd-json", "badgerfish")
            else (data[-1] if format == "jsonml" else data)
        )
    )
    assert scalar == expected
    Draft202012Validator(schema.json_schema(format=format)).validate(data)

    def check(d):
        value = d.root.memberValue
        assert isinstance(value, float)
        if lexical == "NaN":
            assert math.isnan(value)
        else:
            assert value == float(lexical.replace("INF", "inf"))

    assert_xml_roundtrip(schema, schema.from_data(data, format=format), check)


@pytest.mark.parametrize("format", ["pyxsd-json", "badgerfish", "jsonml"])
@pytest.mark.parametrize("shape", ["direct", "list", "union-list", "attribute"])
def test_union_qname_expanded_member_roundtrip(format, shape):
    declarations = """
      <xs:simpleType name="Value"><xs:union memberTypes="xs:boolean xs:QName"/></xs:simpleType>
      <xs:simpleType name="Values"><xs:list itemType="Value"/></xs:simpleType>
      <xs:simpleType name="Choice"><xs:union memberTypes="xs:boolean Values"/></xs:simpleType>"""
    typename = {"direct": "Value", "list": "Values", "union-list": "Choice"}.get(shape)
    root = (
        f'<xs:element name="root" type="{typename}"/>'
        if typename
        else """
      <xs:element name="root"><xs:complexType><xs:attribute name="ref" type="Value"/>
      </xs:complexType></xs:element>"""
    )
    schema = compile_body(declarations + root)
    text = "p:x p:y" if shape in ("list", "union-list") else "p:x"
    xml = (
        f'<root xmlns:p="urn:value" ref="{text}"/>'
        if shape == "attribute"
        else f'<root xmlns:p="urn:value">{text}</root>'
    )
    doc = schema.parse(StringIO(xml))
    data = doc.to_data(format=format, fidelity="strict")
    if format == "pyxsd-json":
        expected = (
            ["Q{urn:value}x", "Q{urn:value}y"]
            if shape in ("list", "union-list")
            else "Q{urn:value}x"
        )
        assert data == {"root": {"@ref": expected} if shape == "attribute" else expected}
    Draft202012Validator(schema.json_schema(format=format)).validate(data)

    def check(d):
        value = d.root.__dict__["ref"] if shape == "attribute" else d.root
        value = getattr(value, "memberValue", value)
        values = value if isinstance(value, list) else [value]
        assert [getattr(v, "memberValue", v).to_expanded_name(strict=True) for v in values] == (
            ["{urn:value}x", "{urn:value}y"]
            if shape in ("list", "union-list")
            else ["{urn:value}x"]
        )

    assert_xml_roundtrip(schema, schema.from_data(data, format=format), check)


@pytest.mark.parametrize("format", ["pyxsd", "parker"])
@pytest.mark.parametrize("shape", ["direct", "list", "attribute", "union"])
def test_grouped_strict_qname_namespace_loss(format, shape):
    prefix = """<xs:simpleType name="Values"><xs:list itemType="xs:QName"/></xs:simpleType>
      <xs:simpleType name="Value"><xs:union memberTypes="xs:boolean xs:QName"/></xs:simpleType>"""
    typename = {"direct": "xs:QName", "list": "Values", "union": "Value"}.get(shape)
    root = (
        f'<xs:element name="root" type="{typename}"/>'
        if typename
        else """
      <xs:element name="root"><xs:complexType><xs:attribute name="ref" type="xs:QName"/>
      </xs:complexType></xs:element>"""
    )
    schema = compile_body(prefix + root)
    xml = (
        '<root xmlns:p="urn:value" ref="p:x"/>'
        if shape == "attribute"
        else '<root xmlns:p="urn:value">p:x</root>'
    )
    doc = schema.parse(StringIO(xml))
    code = "attributes-lost" if format == "parker" and shape == "attribute" else "namespace-lost"
    with pytest.raises(PyXSDError, match=code):
        doc.to_data(format=format, fidelity="strict")


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("root_namespace", ["", "urn:element"])
def test_xsi_type_value_only_namespace_survives_xml(tmp_path, format, root_namespace):
    types = tmp_path / "types.xsd"
    types.write_text("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      targetNamespace="urn:type">
      <xs:complexType name="Derived"><xs:simpleContent><xs:extension base="xs:string"/>
      </xs:simpleContent></xs:complexType>
      </xs:schema>""")
    schema = compile_body(
        f'''<xs:import namespace="urn:type" schemaLocation="{types}"/>
      <xs:element name="root" type="xs:anyType"/>''',
        f'targetNamespace="{root_namespace}"' if root_namespace else "",
    )
    doc = schema.parse(
        StringIO(f'''<root xmlns="{root_namespace}"
       xmlns:ns0="urn:type" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:type="ns0:Derived">text</root>''')
    )
    if format == "parker":
        with pytest.warns(PyXSDWarning):
            data = doc.to_data(format=format)
        return  # Parker explicitly cannot retain an actual-type marker.
    data = doc.to_data(format=format)
    restored = schema.from_data(data, format=format, namespaces={"ns0": "urn:type"})

    def check(d):
        assert type(d.root).name == "Derived"
        assert type(d.root)._targetNamespace_ == "urn:type"
        assert str(d.root) == "text"

    assert_xml_roundtrip(schema, restored, check)


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("marker", ["t:Value", "missing:Value", "not a QName"])
def test_redundant_type_marker_keeps_its_original_resolution(tmp_path, child, marker):
    types = tmp_path / "types.xsd"
    types.write_text("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      targetNamespace="urn:type"><xs:simpleType name="Value"><xs:restriction base="xs:string"/>
      </xs:simpleType></xs:schema>""")
    content = (
        """<xs:element name="root"><xs:complexType><xs:sequence>
      <xs:element name="item" type="t:Value"/></xs:sequence></xs:complexType></xs:element>"""
        if child
        else '<xs:element name="root" type="t:Value"/>'
    )
    schema = compile_body(
        f'<xs:import namespace="urn:type" schemaLocation="{types}"/>' + content,
        'xmlns:t="urn:type"',
    )
    tagged = f'<item xsi:type="{marker}">text</item>' if child else "text"
    root_marker = "" if child else f'xsi:type="{marker}"'
    doc = schema.parse(
        StringIO(f"""<root xmlns:t="urn:type"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" {root_marker}>{tagged}</root>""")
    )
    if marker == "t:Value":

        def check(d):
            node = d.root._children_[0] if child else d.root
            assert type(node).name == "Value"
            assert type(node)._targetNamespace_ == "urn:type"
            assert str(node) == "text"

        assert_xml_roundtrip(schema, doc, check)
        data = doc.to_data()
        restored = schema.from_data(data, format="pyxsd", namespaces={"t": "urn:type"})
        assert_xml_roundtrip(schema, restored, check)
        node = doc.root._children_[0] if child else doc.root
        node._attribs_["xsi:type"] = "missing:Value"
        assert "missing:Value" in doc.to_string()
        assert doc.revalidate().report.has_errors
    else:
        assert doc.report.has_errors
        assert marker in doc.to_string()
        assert doc.revalidate().report.has_errors


def test_unknown_xsi_type_is_not_rewritten_as_the_declared_type():
    schema = compile_body(
        '<xs:element name="root" type="xs:string"/>', 'targetNamespace="urn:root"'
    )
    doc = schema.parse(
        StringIO("""<r:root xmlns:r="urn:root"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="missing:Type">x</r:root>""")
    )
    assert "xsi:type" in doc.to_string()
    assert "missing:Type" in doc.to_string()
    assert any(issue.code == "unknown-namespace-prefix" for issue in doc.revalidate().report.issues)


def test_type_marker_added_without_namespace_evidence_is_not_guessed():
    schema = compile_body('<xs:element name="root" type="xs:anyType"/>')
    doc = schema.parse(StringIO("<root>text</root>"))
    doc.root._attribs_["xsi:type"] = "p:Unknown"
    assert "p:Unknown" in doc.to_string()
    assert any(issue.code == "unknown-namespace-prefix" for issue in doc.revalidate().report.issues)


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_disjoint_roots_use_independent_schema_prefixes(tmp_path, format):
    for ns, name in [("a", "alpha"), ("b", "beta")]:
        (tmp_path / f"{ns}.xsd").write_text(f'''<xs:schema
          xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:{ns}">
          <xs:element name="{name}"><xs:complexType><xs:sequence>
            <xs:element name="child" type="xs:string"/>
          </xs:sequence></xs:complexType></xs:element></xs:schema>''')
    schema = compile_body(f'''<xs:import namespace="urn:a" schemaLocation="{tmp_path}/a.xsd"/>
      <xs:import namespace="urn:b" schemaLocation="{tmp_path}/b.xsd"/>''')
    contract = schema.json_schema(format=format)
    for ns, name in [("a", "alpha"), ("b", "beta")]:
        doc = schema.parse(StringIO(f'<p:{name} xmlns:p="urn:{ns}"><child>x</child></p:{name}>'))
        data = doc.to_data(format=format)
        Draft202012Validator(contract).validate(data)
        Draft202012Validator(
            schema.json_schema(format=format, element=f"{{urn:{ns}}}{name}")
        ).validate(data)


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_substitution_root_alternatives_keep_independent_shared_child_defs(tmp_path, format):
    (tmp_path / "c.xsd").write_text("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      targetNamespace="urn:aa"><xs:element name="child" type="xs:string"/></xs:schema>""")
    (tmp_path / "a.xsd").write_text("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      xmlns:a="urn:a" xmlns:c="urn:aa" targetNamespace="urn:a">
      <xs:import namespace="urn:aa" schemaLocation="c.xsd"/>
      <xs:complexType name="Value"><xs:sequence><xs:element ref="c:child"/>
      </xs:sequence></xs:complexType><xs:element name="head" type="a:Value"/></xs:schema>""")
    (tmp_path / "b.xsd").write_text("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      xmlns:a="urn:a" targetNamespace="urn:b"><xs:import namespace="urn:a" schemaLocation="a.xsd"/>
      <xs:element name="member" type="a:Value" substitutionGroup="a:head"/></xs:schema>""")
    schema = compile_body(f'''<xs:import namespace="urn:a" schemaLocation="{tmp_path}/a.xsd"/>
      <xs:import namespace="urn:b" schemaLocation="{tmp_path}/b.xsd"/>''')
    for selection in (None, "{urn:a}head"):
        contract = schema.json_schema(format=format, element=selection)
        for uri, local in [("urn:a", "head"), ("urn:b", "member")]:
            doc = schema.parse(
                StringIO(
                    f'<p:{local} xmlns:p="{uri}" xmlns:c="urn:aa"><c:child>x</c:child></p:{local}>'
                )
            )
            doc.require_valid()
            data = doc.to_data(format=format, fidelity="strict")
            Draft202012Validator(contract).validate(data)
            restored = schema.from_data(data, format=format, element=selection)
            restored.require_valid()
            assert restored.root._name_ == f"{{{uri}}}{local}"
            assert restored.to_data(format=format) == data


def test_badgerfish_each_occurrence_merges_its_own_frame():
    schema = compile_body("""<xs:element name="root"><xs:complexType><xs:sequence>
      <xs:element name="item" maxOccurs="unbounded"><xs:complexType><xs:simpleContent>
      <xs:extension base="xs:QName"><xs:anyAttribute processContents="skip"/></xs:extension>
      </xs:simpleContent></xs:complexType></xs:element>
      </xs:sequence></xs:complexType></xs:element>""")
    data = {
        "root": {
            "item": [
                {"@xmlns": {"p": uri}, "@p:a": "x", "$": "p:x"} for uri in ("urn:one", "urn:two")
            ]
        }
    }

    def check(d):
        assert [v.to_expanded_name(strict=True) for v in d.root._children_] == [
            "{urn:one}x",
            "{urn:two}x",
        ]
        assert [set(v._attribs_) for v in d.root._children_] == [{"{urn:one}a"}, {"{urn:two}a"}]

    assert_xml_roundtrip(schema, schema.from_data(data, format="badgerfish"), check)


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish"])
@pytest.mark.parametrize("contents", [[], [["a", "b"], ["c"]], [[]], [["a"]]])
def test_grouped_repeated_nested_list_occurrences(format, contents):
    schema = compile_body("""<xs:simpleType name="Words"><xs:list itemType="xs:string"/></xs:simpleType>
      <xs:element name="root"><xs:complexType><xs:sequence>
      <xs:element name="refs" type="Words" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence></xs:complexType></xs:element>""")
    xml = "<root>" + "".join(f"<refs>{' '.join(v)}</refs>" for v in contents) + "</root>"
    doc = schema.parse(StringIO(xml))
    data = doc.to_data(format=format)
    # Native/Parker's single flat occurrence stays genuinely ambiguous. Explicit outer
    # occurrence arrays (native always_list, or nested data) are unambiguous.
    if format == "pyxsd" and contents:
        data = doc.to_data(always_list=True)
        assert data == {"refs": contents}
    elif format == "parker":
        data = {"refs": contents} if contents else {}
    restored = schema.from_data(data, format=format)

    def check(d):
        assert [list(v) for v in d.root._children_] == contents

    assert_xml_roundtrip(schema, restored, check)
    assert restored.to_data(format=format) == doc.to_data(format=format)
    if format == "pyxsd" and contents:
        assert restored.to_data(always_list=True) == data
    if format == "badgerfish" and not contents:
        assert (
            schema.from_data({"root": {"refs": []}}, format=format).to_data(format=format) == data
        )


@pytest.mark.parametrize("format", ["pyxsd-json", "jsonml"])
def test_schema_markers_are_isolated_between_calls(format):
    schema = compile_body('<xs:element name="root" nillable="true" type="xs:anyType"/>')
    before = schema.json_schema(format=format)
    expected = copy.deepcopy(before)

    def mutate(value):
        if isinstance(value, dict):
            for child in list(value.values()):
                mutate(child)
            value["review-mutation"] = True
        elif isinstance(value, list):
            for child in value:
                mutate(child)

    mutate(before)
    assert schema.json_schema(format=format) == expected


@pytest.mark.parametrize("format", ["pyxsd", "parker"])
def test_selected_abstract_root_without_members_has_empty_schema(format):
    schema = compile_body('<xs:element name="root" abstract="true" type="xs:string"/>')
    contract = schema.json_schema(format=format, element="root")
    assert not Draft202012Validator(contract).is_valid("anything")


@pytest.mark.parametrize("format", FORMATS)
@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_direct_python_nonfinite_is_rejected_at_input_boundary(format, number):
    schema = compile_body('<xs:element name="root" type="xs:double"/>')
    data = (
        {"$": number}
        if format == "pyxsd"
        else {"root": number}
        if format in ("pyxsd-json", "badgerfish")
        else ["root", number]
        if format == "jsonml"
        else number
    )
    with pytest.raises(PyXSDError, match="json-invalid"):
        schema.from_data(data, format=format)


def test_native_raw_decimal_keeps_legacy_exponent_spelling():
    assert scalar_to_json(Decimal("1E+3"), format="pyxsd") == "1E+3"
    assert scalar_to_json(Decimal("1E+3"), format="pyxsd-json") == "1000"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_scalar_input_helper_requires_finite_json_numbers(number):
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        scalar_to_lexical(number, datatype=Double, format="pyxsd-json")


def test_mixed_missing_model_is_conservatively_open():
    compile_body('<xs:element name="root" type="xs:string"/>')
    from pyxsd.schema_base import SchemaBase

    class MixedMissingModel(SchemaBase):
        _contentKind_ = "complex"
        _elementOnly_ = False

    plan = plan_layout(MixedMissingModel)
    assert plan.layout is Layout.MIXED
    assert plan.children == ()
    assert plan.has_wildcards is True
    assert plan.has_attributes is False


@pytest.mark.parametrize("format", FORMATS)
def test_import_conversion_errors_identify_format(format):
    schema = compile_body('<xs:element name="root" type="xs:integer"/>')
    data = (
        {"$": True}
        if format == "pyxsd"
        else (
            {"root": True}
            if format in ("pyxsd-json", "badgerfish")
            else ["root", True]
            if format == "jsonml"
            else True
        )
    )
    with pytest.raises(PyXSDError, match="scalar-type-invalid") as error:
        schema.from_data(data, format=format)
    assert f"format={format!r}" in str(error.value)
    with pytest.raises(PyXSDError, match="json-invalid") as error:
        schema.from_json("{bad", format=format)
    assert f"format={format!r}" in str(error.value)


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_import_qname_warning_identifies_format(format):
    schema = compile_body('<xs:element name="root" type="xs:QName"/>')
    data = (
        {"$": "p:x"}
        if format == "pyxsd"
        else (
            {"root": "p:x"}
            if format == "badgerfish"
            else ["root", "p:x"]
            if format == "jsonml"
            else "p:x"
        )
    )
    with pytest.warns(PyXSDWarning, match="namespace-unresolved") as warnings:
        schema.from_data(data, format=format, fidelity="best-effort")
    assert f"format={format!r}" in str(warnings[0].message)


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
@pytest.mark.parametrize("lexical", ["p:x", "p:x p:y"])
def test_union_lexical_qnames_require_context(format, lexical):
    schema = compile_body("""
      <xs:simpleType name="Names"><xs:list itemType="xs:QName"/></xs:simpleType>
      <xs:simpleType name="Value"><xs:union memberTypes="xs:boolean xs:QName Names"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    value = lexical.split() if " " in lexical else lexical
    # Native lexical lists remain accepted as strings; other formats use arrays.
    data = (
        {"$": lexical}
        if format == "pyxsd"
        else (
            {"root": value}
            if format == "badgerfish"
            else ["root", value]
            if format == "jsonml"
            else value
        )
    )
    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        schema.from_data(data, format=format)
    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        schema.from_data(data, format=format, fidelity="best-effort")
    restored = schema.from_data(data, format=format, namespaces={"p": "urn:value"})

    def check(d):
        member = d.root.memberValue
        members = member if isinstance(member, list) else [member]
        assert [v.to_expanded_name(strict=True) for v in members] == [
            "{urn:value}" + token.split(":")[1] for token in lexical.split()
        ]

    assert_xml_roundtrip(schema, restored, check)


@pytest.mark.parametrize("shape", ["direct", "attribute", "list"])
def test_native_strict_resolved_no_namespace_qnames_are_lossless(shape):
    schema = compile_body(
        """<xs:simpleType name="Names"><xs:list itemType="xs:QName"/></xs:simpleType>"""
        + (
            '<xs:element name="root" type="Names"/>'
            if shape == "list"
            else '<xs:element name="root" type="xs:QName"/>'
            if shape == "direct"
            else '<xs:element name="root"><xs:complexType><xs:attribute name="ref" type="xs:QName"/></xs:complexType></xs:element>'
        )
    )
    xml = '<root ref="x"/>' if shape == "attribute" else "<root>x</root>"
    doc = schema.parse(StringIO(xml))
    data = doc.to_data(fidelity="strict")
    assert data == (
        {"@ref": "x"} if shape == "attribute" else {"$": ["x"] if shape == "list" else "x"}
    )
    restored = schema.from_data(data, format="pyxsd")
    restored.require_valid()
    assert restored.to_data(fidelity="strict") == data


def test_native_strict_unresolved_qname_is_not_silently_accepted():
    from pyxsd.data_conversion.export import bound_to_data
    from pyxsd.xsd_data_types import QName

    node = QName("p:x")
    node._name_ = "root"
    node._children_ = []
    node._attribs_ = {}
    node._value_ = ["p:x"]
    node._descriptor_ = None
    with pytest.raises(PyXSDError, match="namespace-lost"):
        bound_to_data(node, fidelity="strict")


def test_badgerfish_sibling_type_markers_use_occurrence_frames(tmp_path):
    for suffix in ("one", "two"):
        (tmp_path / f"{suffix}.xsd").write_text(f"""<xs:schema
          xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:{suffix}">
          <xs:complexType name="Value"><xs:simpleContent><xs:extension base="xs:QName"/>
          </xs:simpleContent></xs:complexType></xs:schema>""")
    schema = compile_body(f'''<xs:import namespace="urn:one" schemaLocation="{tmp_path}/one.xsd"/>
      <xs:import namespace="urn:two" schemaLocation="{tmp_path}/two.xsd"/>
      <xs:element name="root"><xs:complexType><xs:sequence>
      <xs:element name="item" type="xs:anyType" maxOccurs="unbounded"/>
      </xs:sequence></xs:complexType></xs:element>''')
    data = {
        "root": {
            "item": [
                {
                    "@xmlns": {
                        "p": f"urn:{suffix}",
                        "xsi": "http://www.w3.org/2001/XMLSchema-instance",
                    },
                    "@xsi:type": "p:Value",
                    "$": "p:x",
                }
                for suffix in ("one", "two")
            ]
        }
    }

    def check(d):
        assert [type(v)._targetNamespace_ for v in d.root._children_] == ["urn:one", "urn:two"]
        assert [v.to_expanded_name(strict=True) for v in d.root._children_] == [
            "{urn:one}x",
            "{urn:two}x",
        ]

    assert_xml_roundtrip(schema, schema.from_data(data, format="badgerfish"), check)


def test_badgerfish_repeated_prefixed_tags_use_each_occurrence_frame():
    schema = compile_body("""<xs:element name="root"><xs:complexType><xs:sequence>
      <xs:any processContents="skip" maxOccurs="unbounded"/>
      </xs:sequence></xs:complexType></xs:element>""")
    data = {
        "root": {
            "p:item": [{"@xmlns": {"p": f"urn:{suffix}"}, "$": "text"} for suffix in ("one", "two")]
        }
    }
    doc = schema.from_data(data, format="badgerfish")

    def check(d):
        assert [v._name_ for v in d.root._children_] == ["{urn:one}item", "{urn:two}item"]

    assert_xml_roundtrip(schema, doc, check)


@pytest.mark.parametrize("lexical,expected", [("true", True), ("42", 42), ("hello", "hello")])
def test_union_typed_members_are_preserved(lexical, expected):
    schema = compile_body("""<xs:simpleType name="Value"><xs:union memberTypes="xs:boolean xs:int xs:string"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    doc = schema.parse(StringIO(f"<root>{lexical}</root>"))
    assert doc.to_data(format="pyxsd-json") == {"root": expected}
    restored = schema.from_data({"root": expected})
    restored.require_valid()
    assert type(restored.root.memberValue) is type(doc.root.memberValue)
    assert restored.root.memberValue == doc.root.memberValue


def test_union_wrong_scalar_type_is_not_coerced():
    schema = compile_body("""<xs:simpleType name="Value"><xs:union memberTypes="xs:boolean xs:int"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": 1.5})


def test_union_attribute_lexical_edits_are_not_overwritten_by_stale_members():
    schema = compile_body(
        """<xs:simpleType name="Value"><xs:union memberTypes="xs:int xs:boolean"/></xs:simpleType>
      <xs:element name="root"><xs:complexType><xs:attribute name="ref" type="t:Value"/>
      </xs:complexType></xs:element>""",
        'xmlns:t="urn:element" targetNamespace="urn:element"',
    )
    doc = schema.parse(StringIO('<p:root xmlns:p="urn:element" ref="1"/>'))
    doc.require_valid()
    doc.root._attribs_["ref"] = "not a valid integer or boolean"
    assert "not a valid integer or boolean" in doc.to_string()
    assert doc.revalidate().report.has_errors


@pytest.mark.parametrize("as_list", [False, True])
def test_union_element_lexical_edits_are_not_overwritten_by_stale_members(as_list):
    schema = compile_body("""<xs:simpleType name="Value"><xs:union memberTypes="xs:int xs:boolean"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    doc = schema.parse(StringIO("<root>1</root>"))
    doc.root._value_ = ["invalid"] if as_list else "invalid"
    assert "invalid" in doc.to_string()
    assert doc.revalidate().report.has_errors


def test_union_string_lexical_invalidity_is_reported_by_binder():
    schema = compile_body("""<xs:simpleType name="Value"><xs:union memberTypes="xs:date xs:QName"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    doc = schema.from_data({"root": "not a valid date or name"})
    assert doc.report.has_errors


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_conventional_invalid_union_lexical_value_reaches_validation(format):
    schema = compile_body("""<xs:simpleType name="Value"><xs:union memberTypes="xs:date xs:QName"/></xs:simpleType>
      <xs:element name="root" type="Value"/>""")
    text = "not a valid date or name"
    data = (
        {"$": text}
        if format == "pyxsd"
        else (
            {"root": text}
            if format == "badgerfish"
            else ["root", text]
            if format == "jsonml"
            else text
        )
    )
    assert schema.from_data(data, format=format).report.has_errors


def test_namespace_used_only_by_wildcard_attribute_survives_xml():
    schema = compile_body("""<xs:element name="root"><xs:complexType><xs:simpleContent>
      <xs:extension base="xs:string"><xs:anyAttribute processContents="skip"/></xs:extension>
      </xs:simpleContent></xs:complexType></xs:element>""")
    doc = schema.parse(StringIO('<root xmlns:p="urn:attribute" p:a="value">text</root>'))

    def check(d):
        assert d.root._attribs_ == {"{urn:attribute}a": "value"}
        assert str(d.root) == "text"

    assert_xml_roundtrip(schema, doc, check)


@pytest.mark.parametrize("format", FORMATS)
def test_nilled_union_has_no_selected_scalar_to_audit_or_write(format):
    schema = compile_body("""<xs:simpleType name="Value"><xs:union memberTypes="xs:boolean xs:QName"/></xs:simpleType>
      <xs:element name="root" type="Value" nillable="true"/>""")
    doc = schema.parse(
        StringIO("""<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>""")
    )
    doc.require_valid()
    data = doc.to_data(format=format, fidelity="strict")
    restored = schema.from_data(data, format=format)

    def check(d):
        assert d.to_data(format=format) == data
        assert d.root._value_ is None

    assert_xml_roundtrip(schema, restored, check)


def test_authored_single_child_sequence_remains_ordered():
    schema = compile_body("""<xs:element name="root"><xs:complexType><xs:sequence>
      <xs:element name="item" type="xs:string"/>
      </xs:sequence></xs:complexType></xs:element>""")
    doc = schema.parse(StringIO("<root><item>x</item></root>"))
    plan = plan_layout(type(doc.root))
    assert plan.layout is Layout.SEQUENCE
    assert [(v.name, v.min_occurs, v.max_occurs) for v in plan.children] == [("item", 1, 1)]
    assert doc.to_data(format="pyxsd-json") == {"root": [{"item": "x"}]}


def test_block_default_excludes_substitution_members_from_layout():
    schema = compile_body(
        """<xs:element name="head" type="xs:string"/>
      <xs:element name="member" type="xs:string" substitutionGroup="head"/>
      <xs:element name="root"><xs:complexType><xs:sequence>
      <xs:element ref="head" maxOccurs="unbounded"/>
      </xs:sequence></xs:complexType></xs:element>""",
        'blockDefault="substitution"',
    )
    doc = schema.parse(StringIO("<root><head>x</head></root>"))
    doc.require_valid()
    plan = plan_layout(type(doc.root))
    assert plan.layout is Layout.LIST
    assert [v.name for v in plan.children] == ["head"]
    assert doc.to_data(format="pyxsd-json") == {"root": ["x"]}


@pytest.mark.parametrize(
    "attribute", ['xml:lang="en"', 'xsi:schemaLocation="urn:example schema.xsd"']
)
def test_parker_explicit_language_and_schema_hint_loss_is_reported(attribute):
    schema = compile_body("""<xs:element name="root"><xs:complexType><xs:simpleContent>
      <xs:extension base="xs:string"><xs:anyAttribute processContents="skip"/></xs:extension>
      </xs:simpleContent></xs:complexType></xs:element>""")
    doc = schema.parse(
        StringIO(f"""<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      {attribute}>text</root>""")
    )
    doc.require_valid()
    with pytest.raises(PyXSDError, match="attributes-lost"):
        doc.to_data(format="parker", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="attributes-lost"):
        assert doc.to_data(format="parker") == "text"
