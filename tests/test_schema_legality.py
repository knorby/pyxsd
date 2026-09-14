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
