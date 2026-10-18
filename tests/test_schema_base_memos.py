"""Per-class memoization of two bind-time scans.

``_inheritable_attribute_names`` and ``SchemaBase._getElements`` depend only
on the generated class, so their results are cached per class. These tests
pin the observable contract: repeated calls agree (and share the cached
frozenset), callers cannot corrupt the cache through the returned list, and
two distinct classes keep separate entries.
"""

import io

from pyxsd.schema import Schema

SCHEMA_A = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="T">
    <xs:sequence>
      <xs:element name="a" type="xs:string"/>
    </xs:sequence>
    <xs:attribute name="x" type="xs:string" inheritable="true"/>
  </xs:complexType>
</xs:schema>
"""

SCHEMA_B = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="U">
    <xs:sequence>
      <xs:element name="b" type="xs:string"/>
    </xs:sequence>
    <xs:attribute name="y" type="xs:string" inheritable="true"/>
  </xs:complexType>
</xs:schema>
"""


def _class_and_instance(source: str, name: str):
    schema = Schema.compile(io.StringIO(source))
    cls = schema.classes[name]
    return cls, object.__new__(cls)


def test_inheritable_names_repeat_calls_share_the_cached_frozenset():
    from pyxsd.schema_base import _inheritable_attribute_names

    cls, _ = _class_and_instance(SCHEMA_A, "T")
    first = _inheritable_attribute_names(cls)
    second = _inheritable_attribute_names(cls)
    assert first == frozenset({"x"})
    assert first is second


def test_element_descriptors_repeat_calls_agree_and_are_copies():
    _cls, instance = _class_and_instance(SCHEMA_A, "T")
    first = instance._getElements()
    second = instance._getElements()
    assert [descriptor.name for descriptor in first] == ["a"]
    assert [descriptor.name for descriptor in second] == ["a"]
    # The cache must not hand callers the same mutable list.
    assert first is not second
    first.clear()
    assert [descriptor.name for descriptor in instance._getElements()] == ["a"]


def test_distinct_classes_keep_separate_entries():
    from pyxsd.schema_base import _inheritable_attribute_names

    cls_a, instance_a = _class_and_instance(SCHEMA_A, "T")
    cls_b, instance_b = _class_and_instance(SCHEMA_B, "U")
    assert _inheritable_attribute_names(cls_a) == frozenset({"x"})
    assert _inheritable_attribute_names(cls_b) == frozenset({"y"})
    assert [descriptor.name for descriptor in instance_a._getElements()] == ["a"]
    assert [descriptor.name for descriptor in instance_b._getElements()] == ["b"]
