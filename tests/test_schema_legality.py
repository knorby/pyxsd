"""Schema-legality child-grammar checks.

The parser reports illegal, duplicated and misordered child elements of
declarations that carry a table-driven child grammar. These tests pin the
public issue codes and the base mechanism that later tables build on.
"""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD


def _schema_codes(report) -> set[str]:
    return {issue.code for issue in report.for_phase("schema")}


@pytest.fixture
def parse_schema(tmp_path, monkeypatch):
    """Parse a schema without an instance and return the report.

    Mirrors ``tests/xsts/drivers.py::_schema_only_call``: the instance
    phase is stubbed out so a schema declaring no root element can still
    be inspected.
    """
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


class TestChildGrammarInfra:
    def test_unknown_child_reports_declaration_child(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:complexType name='t'>"
            "<xsd:bogus/></xsd:complexType></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_duplicate_single_child_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:complexType name='t'>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_annotation_not_first_reports_declaration_order(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:complexType name='t'>"
            "<xsd:simpleContent><xsd:extension base='xsd:string'/></xsd:simpleContent>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "</xsd:complexType></xsd:schema>"
        )
        assert "declaration-order" in _schema_codes(report)

    def test_permissive_container_does_not_report_annotation_order(self, parse_schema):
        """A foreign child whose local name is ``annotation`` inside a
        permissive container (``sequence`` here) must not fire the
        annotation-order check: the container declares no grammar, so the
        ordering rule does not apply to it.
        """
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema' xmlns:f='urn:foreign'>"
            "<xsd:complexType name='t'><xsd:sequence>"
            "<xsd:element name='a' type='xsd:string'/>"
            "<f:annotation/>"
            "</xsd:sequence></xsd:complexType></xsd:schema>"
        )
        assert "declaration-order" not in _schema_codes(report)


class TestContainerGrammar:
    def test_notation_non_annotation_child_reports_declaration_child(self, parse_schema):
        """notatF002: ``all`` is not a legal child of ``notation``."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='jpeg' public='image/jpeg' system='viewer.exe'>"
            "<xsd:all/>"
            "</xsd:notation></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_notation_foreign_child_reports_declaration_child(self, parse_schema):
        """notatG002: arbitrary foreign XML is not legal notation content."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='jpeg' public='image/jpeg' system='viewer.exe'>"
            "<a><b/></a>"
            "</xsd:notation></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_notation_allows_annotation_child(self, parse_schema):
        """notatF004: an annotation is the one legal notation child."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='foo' public='jpeg' system='system.exe'>"
            "<xsd:annotation/>"
            "</xsd:notation></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_notation_duplicate_annotation_reports_declaration_duplicate(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:notation name='foo' public='jpeg' system='system.exe'>"
            "<xsd:annotation/><xsd:annotation/>"
            "</xsd:notation></xsd:schema>"
        )
        assert "declaration-duplicate" in _schema_codes(report)

    def test_nested_annotation_reports_declaration_child(self, parse_schema):
        """annotB001: an annotation may not contain another annotation."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation>"
            "<xsd:annotation><xsd:appinfo>Application Information</xsd:appinfo></xsd:annotation>"
            "<xsd:appinfo>Application Information</xsd:appinfo>"
            "<xsd:documentation>Documentation</xsd:documentation>"
            "</xsd:annotation></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_annotation_allows_appinfo_and_documentation(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation>"
            "<xsd:appinfo>Application Information</xsd:appinfo>"
            "<xsd:documentation>Documentation</xsd:documentation>"
            "</xsd:annotation></xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_schema_unknown_child_reports_declaration_child(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'><xsd:bogus/></xsd:schema>"
        )
        assert "declaration-child" in _schema_codes(report)

    def test_schema_allows_repeated_annotations(self, parse_schema):
        """A schema may carry more than one top-level annotation (each
        declaration may be preceded by one)."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation/><xsd:annotation/>"
            "<xsd:element name='root' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_schema_allows_trailing_annotation(self, parse_schema):
        """A top-level annotation may also follow the declarations."""
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:element name='root' type='xsd:string'/>"
            "<xsd:annotation/>"
            "</xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)

    def test_schema_annotation_first_is_clean(self, parse_schema):
        report = parse_schema(
            "<xsd:schema xmlns:xsd='http://www.w3.org/2001/XMLSchema'>"
            "<xsd:annotation><xsd:documentation/></xsd:annotation>"
            "<xsd:element name='root' type='xsd:string'/>"
            "</xsd:schema>"
        )
        assert not {
            "declaration-child",
            "declaration-duplicate",
            "declaration-order",
        } & _schema_codes(report)
