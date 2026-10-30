"""A cached Schema must not retain the source tree of its previous parse."""

import gc
import io
import weakref
import xml.etree.ElementTree as ET

import pytest

from pyxsd.schema import Schema

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root"><xs:complexType><xs:sequence>
<xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
</xs:sequence></xs:complexType></xs:element></xs:schema>"""


def test_released_documents_do_not_stay_on_schema_host():
    schema = Schema.compile(io.StringIO(XSD))
    tree = ET.fromstring("<root><item>1</item><item>2</item></root>")
    source = weakref.ref(tree)
    child = weakref.ref(tree[0])
    document = schema.parse(tree)
    assert [int(value) for value in document.root.item] == [1, 2]
    del tree, document
    gc.collect()
    assert source() is None
    assert child() is None
    again = schema.parse(io.StringIO("<root><item>3</item></root>"))
    assert int(again.root.item[0]) == 3


def test_binding_failure_releases_transient_parent_index(monkeypatch):
    import pyxsd.schema as schema_module

    schema = Schema.compile(io.StringIO(XSD))
    tree = ET.fromstring("<root><item>1</item></root>")
    source = weakref.ref(tree)

    def fail(*args):
        raise RuntimeError("binding interrupted")

    monkeypatch.setattr(schema_module, "bind_instance", fail)
    with pytest.raises(RuntimeError, match="binding interrupted"):
        schema.parse(tree)
    del tree
    gc.collect()
    assert source() is None
