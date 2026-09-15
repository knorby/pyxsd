"""Unique Particle Attribution over compiled content models (cos-nonambig).

Unit tests over the pure sweep in ``pyxsd.upa`` plus schema-level tests
for the corpus shapes: the MS particlesZ022/Z033/Z037 clusters and the
ModelGroups mgS002-005/mgQ021 sequence/choice shapes, with valid guards
for the deterministic twins the corpus pins legal.
"""

import pytest

from pyxsd.binding import ParseModes
from pyxsd.content_model import Particle
from pyxsd.upa import upa_violations
from pyxsd.wildcards import WildcardSpec

ANY = WildcardSpec(namespace="##any")
OTHER = WildcardSpec(namespace="##other")


def _elt(name, min_occurs=1, max_occurs=1):
    return Particle("element", min_occurs, max_occurs, [], name=name)


def _group(kind, *children, min_occurs=1, max_occurs=1):
    return Particle(kind, min_occurs, max_occurs, list(children))


class TestSequenceDeterminism:
    def test_univocal_duplicate_is_valid(self):
        # mgQ002: sequence(e1, e1) is deterministic — the first e1 is
        # consumed by the first member exactly once
        model = _group("sequence", _elt("e1"), _elt("e1"))
        assert not upa_violations(model)

    def test_repeated_duplicate_is_invalid(self):
        # particlesZ037's inner sequence: e1(1..5) then e1(1..1)
        model = _group("sequence", _elt("e1", 1, 5), _elt("e1"))
        assert upa_violations(model)

    def test_required_separator_makes_duplicate_deterministic(self):
        # a required particle between the pair fixes the attribution
        model = _group("sequence", _elt("e1", 1, 5), _elt("b"), _elt("e1"))
        assert not upa_violations(model)

    def test_nested_sequence_duplicate_is_invalid(self):
        # particlesZ037's outer shape: the second member is a singleton
        # sequence holding e1(1..5), e1(1..1)
        model = _group(
            "sequence",
            _group("sequence", _elt("e1", 1, 100), _elt("e2")),
            _group("sequence", _elt("e1", 1, 5), _elt("e1")),
        )
        assert upa_violations(model)

    def test_repeated_wildcard_then_overlapping_wildcard_is_invalid(self):
        # particlesZ022's effective extension model: the base wildcard
        # can repeat, so a later overlapping wildcard is ambiguous
        model = _group(
            "sequence",
            _group("sequence", Particle("any", 0, None, [], spec=ANY)),
            _group("choice", Particle("any", 1, 1, [], spec=ANY)),
        )
        assert upa_violations(model)

    def test_single_wildcard_then_disjoint_wildcard_is_valid(self):
        model = _group(
            "sequence",
            Particle("any", 1, 1, [], spec=ANY),
            Particle("any", 1, 1, [], spec=OTHER),
        )
        assert not upa_violations(model)

    def test_element_then_its_wildcard_is_valid(self):
        # the element declaration wins over an overlapping wildcard
        model = _group("sequence", _elt("e1"), Particle("any", 1, 1, [], spec=ANY))
        assert not upa_violations(model)


class TestChoiceDeterminism:
    def test_overlapping_branches_are_invalid(self):
        # two identical branches can never be told apart
        model = _group("choice", _elt("e1"), _elt("e1"))
        assert upa_violations(model)

    def test_disjoint_branches_are_valid(self):
        # mgQ003's effective first sets are disjoint
        model = _group("choice", _group("sequence", _elt("e1"), _elt("e2"), _elt("e1")), _elt("e2"))
        assert not upa_violations(model)

    def test_substitution_member_over_head_is_invalid(self):
        # particlesZ033_e/f: m1 substitutes for head in the same choice
        class _Decl:
            def __init__(self, name, head=None):
                self.name = name
                self.head = head

        member_decl = _Decl("m1", head="head")
        head_decl = _Decl("head")
        member = Particle("element", 1, 1, [], name="m1", descriptor=member_decl)
        head = Particle("element", 1, 1, [], name="head", descriptor=head_decl)
        model = _group("choice", member, head)
        lookup = lambda declaration: head_decl if declaration is member_decl else None  # noqa: E731
        assert upa_violations(model, head_lookup=lookup)

    def test_nested_choice_branch_overlap_is_invalid(self):
        # mgS004: the outer branch sequence(e1, e2) and the inner
        # choice's e1 share a first element
        model = _group(
            "choice",
            _group("sequence", _elt("a"), _elt("b")),
            _group("choice", _elt("a"), _elt("b")),
        )
        assert upa_violations(model)

    def test_same_kind_nested_choice_is_transparent(self):
        # mgS003: choice(sequence(a, b), sequence(a)) — both branches
        # start with a
        model = _group(
            "choice",
            _group("sequence", _elt("a"), _elt("b")),
            _group("sequence", _elt("a")),
        )
        assert upa_violations(model)

    def test_choice_element_then_wildcard_is_valid(self):
        model = _group("choice", _elt("e1"), Particle("any", 1, 1, [], spec=ANY))
        assert not upa_violations(model)


@pytest.fixture
def parse(tmp_path, monkeypatch):
    import io

    from pyxsd.parser import PyXSD

    monkeypatch.setattr(PyXSD, "parseXML", lambda self: None)
    schema_path = tmp_path / "schema.xsd"
    head = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"

    def _parse(schema_string: str):
        schema_path.write_text(head + schema_string + "</xs:schema>", encoding="utf-8")
        return PyXSD(
            io.StringIO("<pyxsd-schema-probe/>"),
            str(schema_path),
            xmlFileOutput=False,
            mode=ParseModes.NAMESPACED,
        ).report

    return _parse


def upa_issues(report) -> list:
    return [issue for issue in report.for_phase("schema") if issue.code == "upa"]


class TestSchemaUniqueParticleAttribution:
    def test_nested_sequence_duplicate_is_invalid(self, parse):
        # particlesZ037 condensed: the inner e1(1..5) before e1(1..1)
        report = parse(
            "<xs:complexType name='fooType'><xs:sequence>"
            "<xs:sequence>"
            "<xs:element name='e1' minOccurs='1' maxOccurs='100'/>"
            "<xs:element name='e2'/></xs:sequence>"
            "<xs:sequence minOccurs='1' maxOccurs='1'>"
            "<xs:element name='e1' minOccurs='1' maxOccurs='5'/>"
            "<xs:element name='e1'/></xs:sequence>"
            "</xs:sequence></xs:complexType>"
        )
        assert upa_issues(report)

    def test_extension_wildcard_overlap_is_invalid(self, parse):
        # particlesZ022: the base any repeats and the suffix any overlaps
        report = parse(
            "<xs:complexType name='T1'><xs:sequence>"
            "<xs:any namespace='urn:someother:ns' processContents='lax'"
            " minOccurs='0' maxOccurs='unbounded'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='T2'><xs:complexContent>"
            "<xs:extension base='T1'>"
            "<xs:choice minOccurs='0' maxOccurs='unbounded'>"
            "<xs:element name='bar' type='xs:string'/>"
            "<xs:any namespace='urn:someother:ns' processContents='lax'/>"
            "</xs:choice></xs:extension></xs:complexContent></xs:complexType>"
        )
        assert upa_issues(report)

    def test_choice_branches_sharing_first_element_is_invalid(self, parse):
        # mgS002 condensed: a (bc | bd)
        report = parse(
            "<xs:complexType name='foo'><xs:sequence>"
            "<xs:element name='a'/>"
            "<xs:choice>"
            "<xs:sequence><xs:element name='b'/><xs:element name='c'/></xs:sequence>"
            "<xs:sequence><xs:element name='b'/><xs:element name='d'/></xs:sequence>"
            "</xs:choice></xs:sequence></xs:complexType>"
        )
        assert upa_issues(report)

    def test_group_ref_choice_branch_overlap_is_invalid(self, parse):
        # mgQ021: the choice's e1 and the referenced sequence's e1
        report = parse(
            "<xs:complexType name='foo'><xs:choice>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:group ref='group'/></xs:choice></xs:complexType>"
            "<xs:group name='group'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/></xs:sequence></xs:group>"
        )
        assert upa_issues(report)

    def test_substitution_overlap_in_choice_is_invalid(self, parse):
        # particlesZ033_e/f: m1 substitutes for head in one choice
        report = parse(
            "<xs:element name='head'/>"
            "<xs:element name='m1' substitutionGroup='head'/>"
            "<xs:complexType name='foo'><xs:choice>"
            "<xs:element ref='m1' minOccurs='36524' maxOccurs='6545657'/>"
            "<xs:element ref='head' minOccurs='3' maxOccurs='6'/>"
            "</xs:choice></xs:complexType>"
        )
        assert upa_issues(report)

    def test_univocal_duplicate_sequence_stays_valid(self, parse):
        # mgQ002: sequence(e1, e1) with the same type is deterministic
        report = parse(
            "<xs:complexType name='foo'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e1' type='xs:string'/>"
            "</xs:sequence></xs:complexType>"
        )
        assert not upa_issues(report)

    def test_choice_with_disjoint_branches_is_valid(self, parse):
        # mgQ003: the first sets are disjoint
        report = parse(
            "<xs:complexType name='foo'><xs:choice>"
            "<xs:sequence>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e2' type='xs:string'/>"
            "<xs:element name='e1' type='xs:string'/>"
            "</xs:sequence>"
            "<xs:element name='e2' type='xs:string'/>"
            "</xs:choice></xs:complexType>"
        )
        assert not upa_issues(report)

    def test_optional_choice_branch_skipped_pair_stays_valid(self, parse):
        # mgQ006: the nested e1 sits under an optional sequence member
        # that cannot be skipped into the later branch
        report = parse(
            "<xs:complexType name='foo'><xs:sequence>"
            "<xs:element name='e1' type='xs:string'/>"
            "<xs:element name='e2' type='xs:string' minOccurs='0'/>"
            "</xs:sequence></xs:complexType>"
        )
        assert not upa_issues(report)
