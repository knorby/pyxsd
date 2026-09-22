"""Tests for ``Document.to_dict`` / ``to_json`` and the ``ToDict`` transform."""

import json
from io import StringIO

import pytest

import pyxsd

SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
        <xs:element name="note" type="xs:string"/>
      </xs:sequence>
      <xs:attribute name="a" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

INSTANCE = '<root a="1"><item>1</item><item>2</item><note>hi</note></root>'


def _doc(source=INSTANCE):
    return pyxsd.Schema.compile(StringIO(SCHEMA)).parse(StringIO(source))


def test_to_dict_basic():
    assert _doc().to_dict() == {"@a": 1, "item": [1, 2], "note": "hi"}


def test_to_dict_always_list():
    doc = _doc('<root a="1"><item>1</item><note>hi</note></root>')
    assert doc.to_dict(always_list=True)["item"] == [1]


def test_to_dict_single_repeat_is_not_a_list():
    doc = _doc('<root a="1"><item>1</item><note>hi</note></root>')
    assert doc.to_dict()["item"] == 1


def test_to_dict_lexical_mode():
    assert _doc().to_dict(typed=False)["item"] == ["1", "2"]
    assert _doc().to_dict(typed=False)["@a"] == "1"


def test_to_json_roundtrip():
    doc = _doc()
    assert json.loads(doc.to_json()) == doc.to_dict()


def test_write_through_reflected():
    doc = _doc()
    doc.root.a = 2  # Task 1 write-through
    assert doc.to_dict()["@a"] == 2


def test_transform_wrapper():
    from pyxsd.transforms import ToDict

    doc = _doc()
    assert doc.transform(ToDict()) == doc.to_dict()


def test_transform_wrapper_with_options():
    from pyxsd.transforms import ToDict

    doc = _doc('<root a="1"><item>1</item><note>hi</note></root>')
    assert doc.transform(ToDict(always_list=True))["item"] == [1]


def test_to_dict_without_root_raises():
    doc = pyxsd.Schema.compile(StringIO(SCHEMA)).parse(StringIO("<memo/>"))
    assert doc.root is None
    with pytest.raises(pyxsd.PyXSDError):
        doc.to_dict()


def test_cli_to_dict_outputs_json(tmp_path, capsys):
    from pyxsd.cli import main

    schema = tmp_path / "schema.xsd"
    instance = tmp_path / "instance.xml"
    schema.write_text(SCHEMA)
    instance.write_text(INSTANCE)

    main(["-i", str(instance), "-s", str(schema), "-t", "ToDict()"])

    assert json.loads(capsys.readouterr().out) == {"@a": 1, "item": [1, 2], "note": "hi"}
