"""Catalogue parsing against small synthetic corpora."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from xsts.catalog import (
    CatalogError,
    load_catalog,
    local_name,
    resolve_reference,
)

TS = "http://www.w3.org/XML/2004/xml-schema-test-suite/"
XLINK = "http://www.w3.org/1999/xlink"

SUITE = f"""<?xml version="1.0"?>
<ts:testSuite xmlns:ts="{TS}" xmlns:xlink="{XLINK}" name="T" releaseDate="2020-01-01">
  <ts:testSetRef xlink:href="meta/a.testSet"/>
  <ts:testSetRef xlink:href="bmeta/b.testSet"/>
</ts:testSuite>
"""

A_SET = f"""<?xml version="1.0"?>
<testSet xmlns="{TS}" xmlns:xlink="{XLINK}" contributor="NIST" name="A" version="1.1">
  <testGroup name="g1">
    <schemaTest name="s1">
      <schemaDocument xlink:href="../data/s1.xsd" role="principal"/>
      <expected validity="valid"/>
      <current status="accepted" date="2004-01-14"/>
    </schemaTest>
    <instanceTest name="i1" version="1.1">
      <instanceDocument xlink:href="../data/i1.xml"/>
      <expected validity="invalid"/>
    </instanceTest>
  </testGroup>
  <testGroup name="g2" xml:base="other/">
    <schemaTest name="s2">
      <schemaDocument xlink:href="cham.xsd"/>
      <expected validity="valid" version="1.1"/>
      <expected validity="invalid" version="1.0"/>
    </schemaTest>
  </testGroup>
</testSet>
"""

B_SET = f"""<?xml version="1.0"?>
<ts:testSet xmlns:ts="{TS}" xmlns:xlink="{XLINK}" contributor="Acme" name="B">
  <ts:testGroup name="g3">
    <ts:schemaTest name="s3">
      <ts:schemaDocument xlink:href="../bdata/b1.xsd"/>
      <ts:schemaDocument xlink:href="../bdata/b2.xsd" role="imported"/>
      <ts:expected validity="valid"/>
    </ts:schemaTest>
    <ts:instanceTest name="i3">
      <ts:instanceDocument xlink:href="../bdata/i3.xml"/>
      <ts:expected validity="valid"/>
    </ts:instanceTest>
  </ts:testGroup>
</ts:testSet>
"""


def write_corpus(root: Path) -> None:
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "bmeta").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "bdata").mkdir(parents=True, exist_ok=True)
    (root / "suite.xml").write_text(SUITE)
    (root / "meta" / "a.testSet").write_text(A_SET)
    (root / "bmeta" / "b.testSet").write_text(B_SET)


def test_load_catalog_reads_both_namespace_styles(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))

    assert catalog.suite.name == "T"
    assert [ts.name for ts in catalog.suite.test_sets] == ["A", "B"]
    assert [ts.contributor for ts in catalog.suite.test_sets] == ["NIST", "Acme"]
    assert catalog.suite.test_sets[0].version == ("1.1",)


def test_document_references_resolve_relative_to_the_metadata_file(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    a_set = catalog.suite.test_sets[0]

    schema = a_set.groups[0].schema_test
    assert schema is not None
    assert schema.documents[0].path == PurePosixPath("data/s1.xsd")
    assert schema.documents[0].role == "principal"
    assert a_set.groups[0].instance_tests[0].document.path == PurePosixPath("data/i1.xml")


def test_expected_and_current_are_parsed(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    schema = catalog.suite.test_sets[0].groups[0].schema_test

    assert schema is not None
    assert [(e.validity, e.version) for e in schema.expected] == [("valid", ())]
    assert schema.current is not None
    assert schema.current.status == "accepted"
    assert schema.current.date == "2004-01-14"
    assert catalog.suite.test_sets[0].groups[0].instance_tests[0].version == ("1.1",)


def test_xml_base_is_honoured(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    group = catalog.suite.test_sets[0].groups[1]

    assert group.schema_test is not None
    assert group.schema_test.documents[0].path == PurePosixPath("meta/other/cham.xsd")


def test_multi_document_schema_test_keeps_order_and_roles(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    schema = catalog.suite.test_sets[1].groups[0].schema_test

    assert schema is not None
    assert [d.path for d in schema.documents] == [
        PurePosixPath("bdata/b1.xsd"),
        PurePosixPath("bdata/b2.xsd"),
    ]
    assert schema.documents[1].role == "imported"


def test_missing_test_set_raises_catalog_error(tmp_path: Path) -> None:
    (tmp_path / "suite.xml").write_text(
        f'<ts:testSuite xmlns:ts="{TS}" xmlns:xlink="{XLINK}" name="T">'
        '<ts:testSetRef xlink:href="absent.testSet"/></ts:testSuite>'
    )
    with pytest.raises(CatalogError):
        load_catalog(PurePosixPath(str(tmp_path)))


def test_resolve_reference_normalises_and_rejects_non_local() -> None:
    base = PurePosixPath("meta")
    assert resolve_reference(base, "../data/x.xsd") == PurePosixPath("data/x.xsd")
    assert resolve_reference(base, "a/./b.xsd#frag") == PurePosixPath("meta/a/b.xsd")
    assert resolve_reference(base, "") is None
    assert resolve_reference(base, None) is None
    assert resolve_reference(base, "http://example.com/x.xsd") is None


def test_local_name_strips_namespace() -> None:
    assert local_name(f"{{{TS}}}testGroup") == "testGroup"
    assert local_name("testGroup") == "testGroup"
