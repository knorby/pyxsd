"""Tests for the pyxsd.Document object API."""

from io import StringIO

import pytest

import pyxsd

SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note" type="Note"/>
  <xs:complexType name="Note">
    <xs:sequence>
      <xs:element name="to" type="xs:string"/>
      <xs:element name="count" type="xs:integer"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""

GOOD = "<note><to>you</to><count>3</count></note>"
BAD = "<note><to>you</to><count>not-an-int</count></note>"
UNKNOWN_ROOT = "<memo><to>x</to><count>1</count></memo>"


def _schema() -> "pyxsd.Schema":
    return pyxsd.Schema.compile(StringIO(SCHEMA))


def test_parse_binds_root():
    doc = _schema().parse(StringIO(GOOD))
    assert doc.root is not None
    assert doc.root._name_ == "note"


def test_instance_report_merged_with_schema_phase():
    doc = _schema().parse(StringIO(GOOD))
    assert not doc.report.has_errors
    assert doc.is_valid
    doc.require_valid()  # must not raise


def test_invalid_document_reported_not_raised():
    doc = _schema().parse(StringIO(BAD))
    assert doc.report.has_errors
    assert not doc.is_valid
    with pytest.raises(pyxsd.ValidationError) as excinfo:
        doc.require_valid()
    assert excinfo.value.report is doc.report


def test_unknown_root_yields_document_with_errors():
    doc = _schema().parse(StringIO(UNKNOWN_ROOT))
    assert doc.root is None
    assert any(i.code == "unknown-root" for i in doc.report)


def test_to_string_round_trips():
    doc = _schema().parse(StringIO(GOOD))
    assert "<note>" in doc.to_string()


def test_write_to_file(tmp_path):
    doc = _schema().parse(StringIO(GOOD))
    dest = tmp_path / "out.xml"
    doc.write(dest)
    assert "<note>" in dest.read_text()


def test_write_without_root_raises():
    doc = _schema().parse(StringIO(UNKNOWN_ROOT))
    with pytest.raises(pyxsd.PyXSDError):
        doc.write(StringIO())


def test_parse_from_element_tree():
    import xml.etree.ElementTree as ET

    doc = _schema().parse(ET.fromstring(GOOD))
    assert doc.root is not None


KEYREF_SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="catalog">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" maxOccurs="unbounded">
          <xs:complexType>
            <xs:attribute name="id" type="xs:string"/>
          </xs:complexType>
        </xs:element>
        <xs:element name="link" minOccurs="0">
          <xs:complexType>
            <xs:attribute name="ref" type="xs:string"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
    <xs:key name="itemKey">
      <xs:selector xpath="item"/>
      <xs:field xpath="@id"/>
    </xs:key>
    <xs:keyref name="linkRef" refer="itemKey">
      <xs:selector xpath="link"/>
      <xs:field xpath="@ref"/>
    </xs:keyref>
  </xs:element>
</xs:schema>
"""

KEYREF_GOOD = '<catalog><item id="a1"/><link ref="a1"/></catalog>'
KEYREF_BAD = '<catalog><item id="a1"/><link ref="no-such-id"/></catalog>'


def test_instance_findings_do_not_contaminate_schema_report():
    """C1 regression: instance-phase findings (identity constraints here)
    must land on the per-parse instance report, never on the shared
    schema report — a leaked violation would make a later valid
    document invalid and grow schema.report across parses.
    """
    schema = pyxsd.Schema.compile(StringIO(KEYREF_SCHEMA))
    before = len(list(schema.report))

    bad = schema.parse(StringIO(KEYREF_BAD))
    assert not bad.is_valid
    assert any(issue.code == "identity-keyref" for issue in bad.report)

    good = schema.parse(StringIO(KEYREF_GOOD))
    assert good.is_valid
    assert len(list(schema.report)) == before


def test_walk_yields_pre_order():
    doc = _schema().parse(StringIO(GOOD))
    names = [n._name_ for n in doc.walk()]
    assert names == ["note", "to", "count"]


def test_transform_callable_returning_tree_wraps_document():
    doc = _schema().parse(StringIO(GOOD))

    def touch(root):
        root._value_ = "touched"
        return root

    out = doc.transform(touch)
    assert isinstance(out, pyxsd.Document)
    assert out is not doc
    assert out.root is doc.root  # same tree object; Document is a new view
    assert out.root._value_ == "touched"


def test_transform_callable_returning_other_passes_through():
    doc = _schema().parse(StringIO(GOOD))
    assert doc.transform(lambda root: 42) == 42
    assert doc.transform(lambda root, sep: sep.join(["a", "b"]), "-") == "a-b"


def test_revalidate_produces_document_with_fresh_report():
    doc = _schema().parse(StringIO(GOOD))
    again = doc.revalidate()
    assert isinstance(again, pyxsd.Document)
    assert again.is_valid
