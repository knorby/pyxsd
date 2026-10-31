"""Per-compile attributeGroup reference resolution and its index.

``AttributeGroupRef._resolves_to_group`` consults a per-compile index of
the schema's global attributeGroup declarations (built once per schema
representative) instead of scanning the whole component table on every
reference. These tests pin resolution behavior (a clean reference, a
dangling one, prefixed and forward forms), the index's lifetime (one
cache per compile, shared by every reference on that schema, never
leaked into a later compile), and the kind filter the scan applied (a
same-named element or type never satisfies an attributeGroup ref).
"""

import io
from collections import Counter

import pytest

from pyxsd.binding import ParseModes
from pyxsd.element_representatives.attribute_group import (
    AttributeGroup,
    _attribute_group_index,
)
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.schema import Schema

XSD_NS_DECL = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

GOOD = f"""<xs:schema {XSD_NS_DECL}>
  <xs:attributeGroup name="G">
    <xs:attribute name="a" type="xs:int"/>
  </xs:attributeGroup>
  <xs:complexType name="T">
    <xs:attributeGroup ref="G"/>
  </xs:complexType>
  <xs:element name="root" type="T"/>
</xs:schema>"""

DANGLING = f"""<xs:schema {XSD_NS_DECL}>
  <xs:complexType name="T">
    <xs:attributeGroup ref="G"/>
  </xs:complexType>
  <xs:element name="root" type="T"/>
</xs:schema>"""

TNS_GOOD = f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:attributeGroup name="ag">
    <xs:attribute name="x" type="xs:int"/>
  </xs:attributeGroup>
  <xs:complexType name="T">
    <xs:attributeGroup ref="t:ag"/>
  </xs:complexType>
  <xs:element name="root" type="t:T"/>
</xs:schema>"""

TNS_DANGLING = f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:complexType name="T">
    <xs:attributeGroup ref="t:missing"/>
  </xs:complexType>
  <xs:element name="root" type="t:T"/>
</xs:schema>"""


def _compile(xsd: str):
    return Schema.compile(io.StringIO(xsd), mode=ParseModes.NAMESPACED)


def _schema_codes(report) -> set[str]:
    return {issue.code for issue in report.for_phase("schema")}


def _ref_site(compiled):
    """The ``T`` type's attributeGroup reference site."""
    t = compiled._host.components.getFromName("T", kind="type")
    (ref,) = t.attributeGroupRefs
    return ref


def _group(compiled, name="G"):
    return compiled._host.components.getFromName(name, kind="attributeGroup")


# ---------------------------------------------------------------------------
# resolution behavior (must hold before and after the index)
# ---------------------------------------------------------------------------


def test_clean_reference_compiles():
    report = _compile(GOOD).report
    assert "unknown-attributeGroup" not in _schema_codes(report)


def test_dangling_reference_reports_unknown_attribute_group():
    report = _compile(DANGLING).report
    assert "unknown-attributeGroup" in _schema_codes(report)


def test_prefixed_reference_in_target_namespace_resolves():
    report = _compile(TNS_GOOD).report
    assert "unknown-attributeGroup" not in _schema_codes(report)


def test_dangling_prefixed_reference_reports():
    report = _compile(TNS_DANGLING).report
    assert "unknown-attributeGroup" in _schema_codes(report)


def test_forward_reference_resolves():
    # The definition may follow the reference in document order; the
    # index is built from the finished component table, not the walk.
    xsd = f"""<xs:schema {XSD_NS_DECL}>
  <xs:complexType name="T">
    <xs:attributeGroup ref="G"/>
  </xs:complexType>
  <xs:attributeGroup name="G">
    <xs:attribute name="a" type="xs:int"/>
  </xs:attributeGroup>
  <xs:element name="root" type="T"/>
</xs:schema>"""
    report = _compile(xsd).report
    assert "unknown-attributeGroup" not in _schema_codes(report)


def test_same_named_element_is_not_a_group():
    # Only attributeGroup-kind components are candidates: a global
    # element named ``G`` must not satisfy ``ref="G"``.
    xsd = f"""<xs:schema {XSD_NS_DECL}>
  <xs:element name="G" type="xs:int"/>
  <xs:complexType name="T">
    <xs:attributeGroup ref="G"/>
  </xs:complexType>
  <xs:element name="root" type="T"/>
</xs:schema>"""
    report = _compile(xsd).report
    assert "unknown-attributeGroup" in _schema_codes(report)


# ---------------------------------------------------------------------------
# the per-compile index
# ---------------------------------------------------------------------------


def test_index_is_shared_within_a_compile():
    compiled = _compile(GOOD)
    ref = _ref_site(compiled)
    schema_er = _group(compiled).getSchema()
    assert ref._resolves_to_group() is True
    index = _attribute_group_index(schema_er)
    # Repeated resolution reuses the same cache object and never
    # rebuilds the candidate list.
    assert ref._resolves_to_group() is True
    assert _attribute_group_index(schema_er) is index
    assert index["candidates"] == [_group(compiled)]


def test_index_buckets_candidates_by_name():
    compiled = _compile(GOOD)
    schema_er = _group(compiled).getSchema()
    index = _attribute_group_index(schema_er)
    assert index["by_name"] == {"G": [_group(compiled)]}


def test_index_does_not_leak_between_compiles():
    good = _compile(GOOD)
    dangling = _compile(DANGLING)
    # The second compile must report the dangling reference even though
    # an identical reference resolved cleanly moments before: no index
    # (and no resolution) leaked across the compile boundary.
    assert "unknown-attributeGroup" not in _schema_codes(good.report)
    assert "unknown-attributeGroup" in _schema_codes(dangling.report)
    good_index = _attribute_group_index(_group(good).getSchema())
    dangling_t = dangling._host.components.getFromName("T", kind="type")
    dangling_index = _attribute_group_index(dangling_t.getSchema())
    assert dangling_index is not good_index
    assert dangling_index["candidates"] == []
    assert _ref_site(dangling)._resolves_to_group() is False


# ---------------------------------------------------------------------------
# class-building target compatibility
# ---------------------------------------------------------------------------


def _same_local_name_groups(tmp_path):
    """Two imported namespaces with one global attributeGroup name."""
    (tmp_path / "a.xsd").write_text(
        f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:attributeGroup name="G">
    <xs:attribute name="from_a" type="xs:int"/>
  </xs:attributeGroup>
</xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "b.xsd").write_text(
        f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:b" xmlns:b="urn:b">
  <xs:attributeGroup name="G">
    <xs:attribute name="from_b" type="xs:int"/>
  </xs:attributeGroup>
</xs:schema>""",
        encoding="utf-8",
    )
    main = f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:main"
    xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a"/>
  <xs:import namespace="urn:b"/>
  <xs:complexType name="T"><xs:attributeGroup ref="a:G"/></xs:complexType>
  <xs:complexType name="U"><xs:attributeGroup ref="b:G"/></xs:complexType>
</xs:schema>"""
    return main, {"urn:a": str(tmp_path / "a.xsd"), "urn:b": str(tmp_path / "b.xsd")}


def _compile_same_local_groups(tmp_path, mode):
    main, namespace_schemas = _same_local_name_groups(tmp_path)
    return Schema.compile(io.StringIO(main), mode=mode, namespace_schemas=namespace_schemas)


def _type(compiled, name):
    return compiled._host.components.getFromName(
        name, kind="type", namespace="urn:main", warn=False
    )


def test_same_local_groups_keep_strict_target_selection(tmp_path):
    compiled = _compile_same_local_groups(tmp_path, ParseModes.NAMESPACED)

    assert list(_type(compiled, "T").attributes) == ["from_a"]
    assert list(_type(compiled, "U").attributes) == ["from_b"]
    assert not compiled.report.errors


def test_same_local_groups_keep_legacy_target_selection(tmp_path):
    # Legacy composition merges same-local-name groups; this pins the
    # current class-building result and its two declaration reports.
    compiled = _compile_same_local_groups(tmp_path, ParseModes.STRICT)

    assert list(_type(compiled, "T").attributes) == []
    assert list(_type(compiled, "U").attributes) == ["from_b"]
    assert Counter(issue.code for issue in compiled.report.errors) == Counter(
        {"unknown-attributeGroup": 2}
    )


def _redefined_group_schema(tmp_path):
    (tmp_path / "base.xsd").write_text(
        f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:attributeGroup name="G">
    <xs:attribute name="original" type="xs:int"/>
  </xs:attributeGroup>
</xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "main.xsd").write_text(
        f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:redefine schemaLocation="base.xsd">
    <xs:attributeGroup name="G">
      <xs:attribute name="redefined" type="xs:int"/>
    </xs:attributeGroup>
  </xs:redefine>
  <xs:complexType name="T"><xs:attributeGroup ref="t:G"/></xs:complexType>
</xs:schema>""",
        encoding="utf-8",
    )
    return tmp_path / "main.xsd"


@pytest.mark.parametrize("mode", [ParseModes.NAMESPACED, ParseModes.STRICT])
def test_redefined_group_keeps_class_building_target(tmp_path, mode):
    compiled = Schema.compile(str(_redefined_group_schema(tmp_path)), mode=mode)
    t = compiled._host.components.getFromName("T", kind="type", namespace="urn:t", warn=False)

    assert list(t.attributes) == ["redefined"]


def test_dangling_reference_preserves_repeated_diagnostics():
    compiled = _compile(DANGLING)

    assert Counter(issue.code for issue in compiled.report.errors) == Counter(
        {"unknown-attributeGroup": 2}
    )
    assert not hasattr(_ref_site(compiled), "_resolvedAttributeGroup")


def test_unbound_prefix_reference_preserves_repeated_diagnostics():
    xsd = f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:t" xmlns:t="urn:t">
  <xs:complexType name="T">
    <xs:attributeGroup ref="missing:G"/>
  </xs:complexType>
  <xs:element name="root" type="t:T"/>
</xs:schema>"""
    compiled = _compile(xsd)

    assert Counter(issue.code for issue in compiled.report.errors) == Counter(
        {"unknown-namespace-prefix": 2, "unknown-attributeGroup": 2}
    )
    assert not hasattr(_ref_site(compiled), "_resolvedAttributeGroup")


# ---------------------------------------------------------------------------
# successful-resolution caching
# ---------------------------------------------------------------------------

NESTED = f"""<xs:schema {XSD_NS_DECL}>
  <xs:attributeGroup name="inner">
    <xs:attribute name="nested_value" type="xs:int"/>
  </xs:attributeGroup>
  <xs:attributeGroup name="outer">
    <xs:attributeGroup ref="inner"/>
  </xs:attributeGroup>
  <xs:complexType name="T1"><xs:attributeGroup ref="outer"/></xs:complexType>
  <xs:complexType name="T2"><xs:attributeGroup ref="outer"/></xs:complexType>
</xs:schema>"""


def test_successful_reference_resolves_once_across_compile_phases(monkeypatch):
    calls = []
    original = AttributeGroup.resolveReference

    def recording(self, value, candidates, *, parser=None):
        calls.append(self)
        return original(self, value, candidates, parser=parser)

    monkeypatch.setattr(AttributeGroup, "resolveReference", recording)
    compiled = _compile(GOOD)

    ref = _ref_site(compiled)
    assert calls == [ref]
    assert ref._resolvedAttributeGroup is _group(compiled)
    assert ref._resolve_group() is _group(compiled)
    assert calls == [ref]  # the explicit cache hit does not resolve again


def test_nested_reference_reuse_resolves_each_site_once(monkeypatch):
    calls = []
    originalResolve = AttributeGroup.resolveReference

    def recording(self, value, candidates, *, parser=None):
        calls.append(self)
        return originalResolve(self, value, candidates, parser=parser)

    scans = []
    originalCandidates = ElementRepresentative._globalAttributeGroupCandidates

    def counting(self, *, parser=None):
        scans.append(self)
        return originalCandidates(self, parser=parser)

    monkeypatch.setattr(AttributeGroup, "resolveReference", recording)
    monkeypatch.setattr(ElementRepresentative, "_globalAttributeGroupCandidates", counting)
    compiled = _compile(NESTED)

    components = compiled._host.components
    t1 = components.getFromName("T1", kind="type")
    t2 = components.getFromName("T2", kind="type")
    (t1_ref,) = t1.attributeGroupRefs
    (t2_ref,) = t2.attributeGroupRefs
    outer = components.getFromName("outer", kind="attributeGroup")
    (nested_ref,) = outer.attributeGroupRefs

    assert Counter(calls) == Counter({t1_ref: 1, t2_ref: 1, nested_ref: 1})
    assert len(scans) == 1
    assert "nested_value" in t1.attributes
    assert "nested_value" in t2.attributes


def test_identical_lexical_refs_in_namespace_contexts_keep_distinct_targets(tmp_path):
    # Two documents spell the same reference text ``G``; each default
    # namespace makes its QName name that document's own global group.
    # A cache keyed by lexical ``ref`` text would collapse them.
    for ns, attribute, type_name in (
        ("urn:a", "from_a", "TA"),
        ("urn:b", "from_b", "TB"),
    ):
        (tmp_path / f"{type_name}.xsd").write_text(
            f"""<xs:schema {XSD_NS_DECL} targetNamespace="{ns}" xmlns="{ns}">
  <xs:attributeGroup name="G">
    <xs:attribute name="{attribute}" type="xs:int"/>
  </xs:attributeGroup>
  <xs:complexType name="{type_name}">
    <xs:attributeGroup ref="G"/>
  </xs:complexType>
</xs:schema>""",
            encoding="utf-8",
        )
    main = f"""<xs:schema {XSD_NS_DECL} targetNamespace="urn:main"
    xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a"/>
  <xs:import namespace="urn:b"/>
</xs:schema>"""
    compiled = Schema.compile(
        io.StringIO(main),
        mode=ParseModes.NAMESPACED,
        namespace_schemas={
            "urn:a": str(tmp_path / "TA.xsd"),
            "urn:b": str(tmp_path / "TB.xsd"),
        },
    )

    components = compiled._host.components
    ta = components.getFromName("TA", kind="type", namespace="urn:a", warn=False)
    tb = components.getFromName("TB", kind="type", namespace="urn:b", warn=False)
    (ta_ref,) = ta.attributeGroupRefs
    (tb_ref,) = tb.attributeGroupRefs

    assert ta_ref.ref == tb_ref.ref == "G"
    assert list(ta.attributes) == ["from_a"]
    assert list(tb.attributes) == ["from_b"]
    assert ta_ref._resolvedAttributeGroup is not tb_ref._resolvedAttributeGroup
    assert not compiled.report.errors


def test_successful_reference_cache_does_not_leak_between_compiles(monkeypatch):
    calls = []
    original = AttributeGroup.resolveReference

    def recording(self, value, candidates, *, parser=None):
        calls.append(self)
        return original(self, value, candidates, parser=parser)

    monkeypatch.setattr(AttributeGroup, "resolveReference", recording)
    first = _compile(GOOD)
    second = _compile(GOOD)

    first_ref = _ref_site(first)
    second_ref = _ref_site(second)
    assert first_ref is not second_ref
    assert calls == [first_ref, second_ref]
    assert first_ref._resolvedAttributeGroup is _group(first)
    assert second_ref._resolvedAttributeGroup is _group(second)
    assert first_ref._resolvedAttributeGroup is not second_ref._resolvedAttributeGroup
