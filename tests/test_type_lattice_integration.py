"""Integration tests for the full XSD built-in type lattice at parse time."""

import pytest

from conftest import run_parser
from pyxsd.xsd_data_types import (
    AnyURI,
    Byte,
    Date,
    DateTime,
    Decimal,
    Duration,
    GYear,
    HexBinary,
    Int,
    Long,
    NegativeInteger,
    Short,
    Time,
    UnsignedByte,
)


@pytest.fixture(scope="module")
def root():
    doc = run_parser("datatypes")
    return doc.root


def child_by_name(instance, name):
    """Find a direct child instance by ``_name_`` (see test_generated_classes)."""
    for child in instance._children_:
        if getattr(child, "_name_", None) == name:
            return child
    raise AssertionError(f"no child named {name!r}")


def test_temporal_types(root):
    assert child_by_name(root, "when") == "2006-08-30"
    assert isinstance(child_by_name(root, "when"), Date)
    assert isinstance(child_by_name(root, "moment"), DateTime)
    assert isinstance(child_by_name(root, "alarm"), Time)
    assert isinstance(child_by_name(root, "year"), GYear)
    assert isinstance(child_by_name(root, "span"), Duration)


def test_numeric_types(root):
    assert isinstance(child_by_name(root, "price"), Decimal)
    assert str(child_by_name(root, "price")) == "19.95"
    assert isinstance(child_by_name(root, "population"), Long)
    assert child_by_name(root, "population") == 6700000000
    assert isinstance(child_by_name(root, "offset"), Int)
    assert isinstance(child_by_name(root, "level"), Short)
    assert isinstance(child_by_name(root, "code"), Byte)
    assert child_by_name(root, "code") == 120
    assert isinstance(child_by_name(root, "quantity"), UnsignedByte)
    assert isinstance(child_by_name(root, "debt"), NegativeInteger)
    assert child_by_name(root, "debt") == -500


def test_other_types(root):
    assert isinstance(child_by_name(root, "checksum"), HexBinary)
    assert child_by_name(root, "checksum") == "00FF10"
    assert isinstance(child_by_name(root, "website"), AnyURI)


class TestInvalidTypedValue:
    def make_instance(self, tmp_path, bad):
        schema = """<?xml version="1.0"?>
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="sizes">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="count" type="xs:int"/>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """
        instance = (
            '<sizes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:noNamespaceSchemaLocation="schema.xsd">'
            f"<count>{bad}</count>"
            "</sizes>"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text(instance)

    @pytest.mark.parametrize("bad", ["abc", "3.5", "2147483648", ""])
    def test_invalid_value_reported_not_fatal(self, tmp_path, bad):
        """Bad lexical values become report errors; parsing continues."""
        self.make_instance(tmp_path, bad)

        from pyxsd.schema import Schema

        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        root = doc.root
        assert doc.report.has_errors
        assert any(issue.code == "value" for issue in doc.report.errors), doc.report
        # the invalid child is skipped, the rest of the tree survives
        assert root is not None

    def test_invalid_value_strict_exit(self, tmp_path):
        self.make_instance(tmp_path, "abc")

        from pyxsd import cli

        with pytest.raises(SystemExit) as exc:
            cli.main(
                [
                    "-i",
                    str(tmp_path / "instance.xml"),
                    "-s",
                    str(tmp_path / "schema.xsd"),
                    "--strict",
                ]
            )
        assert exc.value.code == 1

    def test_valid_strict_passes(self, tmp_path):
        self.make_instance(tmp_path, "5")

        from pyxsd import cli

        try:
            cli.main(
                [
                    "-i",
                    str(tmp_path / "instance.xml"),
                    "-s",
                    str(tmp_path / "schema.xsd"),
                    "--strict",
                ]
            )
        except SystemExit as e:  # pragma: no cover - defensive
            pytest.fail(f"valid instance failed strict mode: {e}")
