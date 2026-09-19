"""Substitution-group mechanics pinned by the Area F work.

Covers XSD 1.1 multi-head ``substitutionGroup`` lists, derivation blocks
on a member's type, Element Declarations Consistent across substitution
members, the presence-based ``xsi:nil`` rule, and union-member
``xsi:type`` admission.
"""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD
from pyxsd.validation import IssueSeverity


def _schema_codes(report) -> set[str]:
    return {issue.code for issue in report.for_phase("schema")}


@pytest.fixture
def parse_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    schema_path = tmp_path / "schema.xsd"

    def _parse(schema_string: str):
        schema_path.write_text(schema_string, encoding="utf-8")
        return PyXSD(
            io.StringIO("<pyxsd-schema-probe/>"),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        ).report

    return _parse


@pytest.fixture
def parse_document(tmp_path):
    def _parse(schema_string: str, instance_string: str):
        schema_path = tmp_path / "schema.xsd"
        instance_path = tmp_path / "instance.xml"
        schema_path.write_text(schema_string, encoding="utf-8")
        instance_path.write_text(instance_string, encoding="utf-8")
        return PyXSD(
            str(instance_path),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        ).report

    return _parse


class TestMultiHeadSubstitution:
    def test_member_in_two_heads_registers(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
            "elementFormDefault='qualified'>"
            "<xsd:element name='a' abstract='true'/>"
            "<xsd:element name='b' abstract='true'/>"
            "<xsd:element name='m' substitutionGroup='a b' type='xsd:string'/>"
            "<xsd:element name='root'><xsd:complexType><xsd:sequence>"
            "<xsd:element ref='a'/></xsd:sequence></xsd:complexType></xsd:element>"
            "</xsd:schema>"
        )
        assert "unknown-substitution-head" not in _schema_codes(report)

    def test_multi_head_type_conflict_reported(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='strhead' type='xsd:string'/>"
            "<xsd:element name='inthead' type='xsd:integer'/>"
            "<xsd:element name='m' substitutionGroup='strhead inthead' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert "substitution-type" in _schema_codes(report)


class TestSubstitutionDerivationBlock:
    def test_blocked_member_rejected(self, parse_document):
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
            "elementFormDefault='qualified'>"
            "<xsd:complexType name='Base'><xsd:sequence>"
            "<xsd:element name='x' type='xsd:string' minOccurs='0'/>"
            "</xsd:sequence></xsd:complexType>"
            "<xsd:complexType name='Derived'><xsd:complexContent>"
            "<xsd:extension base='Base'><xsd:sequence>"
            "<xsd:element name='y' type='xsd:string' minOccurs='0'/>"
            "</xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType>"
            "<xsd:element name='h' type='Base' block='extension'/>"
            "<xsd:element name='m' substitutionGroup='h' type='Derived'/>"
            "<xsd:element name='root'><xsd:complexType><xsd:sequence>"
            "<xsd:element ref='h'/></xsd:sequence></xsd:complexType></xsd:element>"
            "</xsd:schema>",
            # the blocked member may not stand in for its head
            "<root><m/></root>",
        )
        assert "blocked" in {issue.code for issue in report.issues}

    def test_head_type_block_rejects_restriction_member(self, parse_document):
        # SUN disallowedSubst00503m3: blockDefault/block on the head's
        # *type* excludes a member whose derivation uses that method.
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
            "xmlns:t='urn:t' targetNamespace='urn:t' elementFormDefault='qualified'>"
            "<xsd:element name='root'><xsd:complexType><xsd:sequence>"
            "<xsd:element ref='t:Head'/>"
            "</xsd:sequence></xsd:complexType></xsd:element>"
            "<xsd:element name='Head' type='t:Type'/>"
            "<xsd:complexType name='Type' block='restriction'/>"
            "<xsd:complexType name='derivedFromType'><xsd:complexContent>"
            "<xsd:restriction base='t:Type'/>"
            "</xsd:complexContent></xsd:complexType>"
            "<xsd:element name='Member1' type='t:derivedFromType' "
            "substitutionGroup='t:Head'/>"
            "</xsd:schema>",
            "<t:root xmlns:t='urn:t'><t:Member1/></t:root>",
        )
        assert "blocked" in {issue.code for issue in report.issues}

    def test_simple_content_extension_member_is_blocked(self, parse_document):
        # MS elemT065: a simpleContent extension of the head's simple type
        # is an extension step, so head block="extension" excludes it.
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
            "elementFormDefault='qualified'>"
            "<xsd:simpleType name='A'><xsd:restriction base='xsd:int'>"
            "<xsd:enumeration value='1'/></xsd:restriction></xsd:simpleType>"
            "<xsd:complexType name='E-A'><xsd:simpleContent>"
            "<xsd:extension base='A'>"
            "<xsd:attribute name='att' type='xsd:int'/>"
            "</xsd:extension></xsd:simpleContent></xsd:complexType>"
            "<xsd:element name='root'><xsd:complexType><xsd:sequence>"
            "<xsd:element ref='test1' minOccurs='0'/>"
            "</xsd:sequence></xsd:complexType></xsd:element>"
            "<xsd:element name='test1' type='A' block='extension'/>"
            "<xsd:element name='sa2' type='E-A' substitutionGroup='test1'/>"
            "</xsd:schema>",
            "<root><sa2 att='1'>1</sa2></root>",
        )
        assert "blocked" in {issue.code for issue in report.issues}

    def test_local_declaration_sharing_head_name_admits_no_member(self, parse_document):
        # MS elemZ021b/f/g, elemZ023: a *local* declaration that shares a
        # global head's name is not that head, so its substitution-group
        # members are not admissible where the local declaration is used.
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
            "xmlns:f='urn:f' targetNamespace='urn:f' elementFormDefault='qualified'>"
            "<xsd:element name='root'><xsd:complexType><xsd:sequence>"
            "<xsd:element name='e' type='xsd:string'/>"
            "</xsd:sequence></xsd:complexType></xsd:element>"
            "<xsd:element name='e'/>"
            "<xsd:element name='e1' type='xsd:int' substitutionGroup='f:e'/>"
            "</xsd:schema>",
            "<f:root xmlns:f='urn:f'><f:e1>123</f:e1></f:root>",
        )
        assert any(issue.severity is IssueSeverity.ERROR for issue in report.issues)

    def test_reference_to_global_head_admits_member(self, parse_document):
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
            "xmlns:f='urn:f' targetNamespace='urn:f' elementFormDefault='qualified'>"
            "<xsd:element name='root'><xsd:complexType><xsd:sequence>"
            "<xsd:element ref='f:e'/>"
            "</xsd:sequence></xsd:complexType></xsd:element>"
            "<xsd:element name='e'/>"
            "<xsd:element name='e1' type='xsd:int' substitutionGroup='f:e'/>"
            "</xsd:schema>",
            "<f:root xmlns:f='urn:f'><f:e1>123</f:e1></f:root>",
        )
        assert not [i for i in report.errors if i.code != "schema-hint"]

    def test_untyped_substitution_member_adopts_head_type(self, parse_document):
        # SUN typeDef00204m: an untyped declaration that is a substitution
        # member takes the head's type definition.
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='Head' type='xsd:boolean'/>"
            "<xsd:element name='root' substitutionGroup='Head'/>"
            "</xsd:schema>",
            "<root>Yes</root>",
        )
        assert any(issue.severity is IssueSeverity.ERROR for issue in report.issues)


class TestElementDeclarationsConsistent:
    _TEMPLATE = (
        "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' "
        "xmlns:t='urn:t' targetNamespace='urn:t' elementFormDefault='qualified'>"
        "<xsd:element name='e' type='xsd:string'/>"
        "<xsd:element name='e1' substitutionGroup='t:e' abstract='true'/>"
        "<xsd:complexType name='T'><xsd:sequence>"
        "<xsd:element ref='t:e'/>"
        "<xsd:element name='e1' type='xsd:integer'{form}/>"
        "</xsd:sequence></xsd:complexType>"
        "<xsd:element name='root' type='t:T'/>"
        "</xsd:schema>"
    )

    def test_member_type_conflict_reported(self, parse_schema):
        report = parse_schema(self._TEMPLATE.format(form=""))
        assert "element-consistent" in _schema_codes(report)

    def test_unqualified_local_no_collision(self, parse_schema):
        report = parse_schema(self._TEMPLATE.format(form=" form='unqualified'"))
        assert "element-consistent" not in _schema_codes(report)


class TestNilPresence:
    def test_nil_false_non_nillable_error(self, parse_document):
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='xsd:string' nillable='false'/>"
            "</xsd:schema>",
            "<root xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' xsi:nil='false'/>",
        )
        assert "nil" in {issue.code for issue in report.for_phase("instance")}

    def test_nil_true_nillable_clean(self, parse_document):
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='xsd:string' nillable='true'/>"
            "</xsd:schema>",
            "<root xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' xsi:nil='true'/>",
        )
        assert not [
            issue for issue in report.for_phase("instance") if issue.severity is IssueSeverity.ERROR
        ]


class TestUnionMemberXsiType:
    def test_union_member_override_valid(self, parse_document):
        report = parse_document(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:simpleType name='A'><xsd:restriction base='xsd:string'/></xsd:simpleType>"
            "<xsd:simpleType name='UnionA'><xsd:union memberTypes='A'/></xsd:simpleType>"
            "<xsd:element name='root' type='UnionA'/>"
            "</xsd:schema>",
            "<root xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' xsi:type='A'>x</root>",
        )
        assert "xsi-type" not in {issue.code for issue in report.for_phase("instance")}
