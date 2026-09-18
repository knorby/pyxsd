"""Case building and execution with a scripted driver."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from xsts.catalog import DocumentRef, load_catalog
from xsts.drivers import PyXSDDriver, build_bundle
from xsts.outcomes import EngineResult, Outcome
from xsts.runner import (
    INSTANCE,
    SCHEMA,
    Runner,
    build_cases,
    corpus_available,
    load_default_catalog,
)
from xsts.selection import XSD10, XSD11

TS = "http://www.w3.org/XML/2004/xml-schema-test-suite/"
XLINK = "http://www.w3.org/1999/xlink"
XS = "http://www.w3.org/2001/XMLSchema"

SUITE = f"""<ts:testSuite xmlns:ts="{TS}" xmlns:xlink="{XLINK}" name="T">
  <ts:testSetRef xlink:href="meta/a.testSet"/>
</ts:testSuite>
"""

A_SET = f"""<testSet xmlns="{TS}" xmlns:xlink="{XLINK}" contributor="NIST" name="A">
  <testGroup name="ok">
    <schemaTest name="s"><schemaDocument xlink:href="../data/s.xsd"/>
      <expected validity="valid"/></schemaTest>
    <instanceTest name="i"><instanceDocument xlink:href="../data/i.xml"/>
      <expected validity="invalid"/></instanceTest>
  </testGroup>
  <testGroup name="tagged" version="1.1">
    <schemaTest name="s2"><schemaDocument xlink:href="../data/s2.xsd"/>
      <expected validity="valid"/></schemaTest>
  </testGroup>
  <testGroup name="noschema">
    <instanceTest name="i2"><instanceDocument xlink:href="../data/i2.xml"/>
      <expected validity="valid"/></instanceTest>
  </testGroup>
  <testGroup name="noschema-ns">
    <instanceTest name="i3"><instanceDocument xlink:href="../data/i3.xml"/>
      <expected validity="valid"/></instanceTest>
  </testGroup>
  <testGroup name="noschema-typed">
    <instanceTest name="i4"><instanceDocument xlink:href="../data/i4.xml"/>
      <expected validity="valid"/></instanceTest>
  </testGroup>
  <testGroup name="noschema-hinted">
    <instanceTest name="i5"><instanceDocument xlink:href="../data/i5.xml"/>
      <expected validity="valid"/></instanceTest>
  </testGroup>
  <testGroup name="redef">
    <schemaTest name="rs">
      <schemaDocument xlink:href="../data/redefine.xsd"/>
      <schemaDocument xlink:href="../data/redefined.xsd"/>
      <expected validity="valid"/>
    </schemaTest>
    <instanceTest name="ri"><instanceDocument xlink:href="../data/ri.xml"/>
      <expected validity="valid"/></instanceTest>
  </testGroup>
  <testGroup name="imports">
    <schemaTest name="is">
      <schemaDocument xlink:href="../data/importer.xsd"/>
      <schemaDocument xlink:href="../data/imported.xsd"/>
      <expected validity="valid"/>
    </schemaTest>
  </testGroup>
  <testGroup name="independent">
    <schemaTest name="x">
      <schemaDocument xlink:href="../data/left.xsd"/>
      <schemaDocument xlink:href="../data/right.xsd"/>
      <expected validity="valid"/>
    </schemaTest>
  </testGroup>
</testSet>
"""


class ScriptedDriver:
    """Returns pre-scripted verdicts keyed by test id; records what was asked."""

    name = "scripted"
    #: Opt-in synthesis of a permissive schema for no-schema groups.
    synthesize_missing_schema = False

    def __init__(
        self,
        schema: dict[str, bool],
        instance: dict[str, bool],
        *,
        synthesize_missing_schema: bool = False,
    ) -> None:
        self.schema = schema
        self.instance = instance
        self.synthesize_missing_schema = synthesize_missing_schema
        self.calls: list[tuple[str, str, str]] = []

    def compile_schema(self, schema_path: Path) -> EngineResult:
        self.calls.append(("schema", str(schema_path), ""))
        return EngineResult(schema_valid=self.schema.get("default", True))

    def validate(self, schema_path: Path, instance_path: Path) -> EngineResult:
        self.calls.append(("instance", str(schema_path), str(instance_path)))
        return EngineResult(
            schema_valid=self.schema.get("default", True),
            instance_valid=self.instance.get(instance_path.name, True),
        )


class SchemaFailingDriver(ScriptedDriver):
    """A driver whose schema phase always fails (adapter-gap reason set)."""

    def validate(self, schema_path: Path, instance_path: Path) -> EngineResult:
        self.calls.append(("instance", str(schema_path), str(instance_path)))
        return EngineResult(schema_valid=False, adapter_gap="group schema did not compile")


def write_corpus(root: Path) -> None:
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "suite.xml").write_text(SUITE)
    (root / "meta" / "a.testSet").write_text(A_SET)
    for name in ("s.xsd", "s2.xsd"):
        (root / "data" / name).write_text("<x/>")
    for name in ("i.xml", "i2.xml"):
        (root / "data" / name).write_text("<x/>")
    (root / "data" / "i3.xml").write_text('<p:root xmlns:p="urn:probe"/>')
    (root / "data" / "i4.xml").write_text(
        '<n xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xsi:type="xs:int">not-an-int</n>'
    )
    (root / "data" / "i5.xml").write_text(
        '<h xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="h.xsd"/>'
    )
    (root / "data" / "h.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}"><xs:element name="h" type="xs:string"/></xs:schema>'
    )
    (root / "data" / "redefined.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:r" xmlns:r="urn:r">'
        '<xs:complexType name="c1"><xs:sequence><xs:element name="e1"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:element name="root" type="r:c1"/>'
        "</xs:schema>"
    )
    (root / "data" / "redefine.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:r" xmlns:r="urn:r">'
        '<xs:redefine schemaLocation="redefined.xsd">'
        '<xs:complexType name="c1"><xs:complexContent><xs:extension base="r:c1">'
        '<xs:sequence><xs:element name="added"/></xs:sequence>'
        "</xs:extension></xs:complexContent></xs:complexType>"
        "</xs:redefine>"
        "</xs:schema>"
    )
    (root / "data" / "ri.xml").write_text('<root xmlns="urn:r"><e1/><added/></root>')
    (root / "data" / "imported.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:j" xmlns:j="urn:j">'
        '<xs:element name="j" type="xs:string"/>'
        "</xs:schema>"
    )
    (root / "data" / "importer.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:i" xmlns:i="urn:i">'
        '<xs:import namespace="urn:j" schemaLocation="imported.xsd"/>'
        '<xs:element name="i" type="xs:string"/>'
        "</xs:schema>"
    )
    (root / "data" / "left.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:l" xmlns:l="urn:l">'
        '<xs:element name="left" type="xs:string"/>'
        "</xs:schema>"
    )
    (root / "data" / "right.xsd").write_text(
        f'<xs:schema xmlns:xs="{XS}" targetNamespace="urn:rr" xmlns:rr="urn:rr">'
        '<xs:element name="right" type="xs:string"/>'
        "</xs:schema>"
    )


def test_build_cases_covers_schema_and_instance(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = build_cases(catalog, XSD11)

    kinds = {(case.kind, case.test_id.split("/")[-1]) for case in cases}
    assert (SCHEMA, "s") in kinds
    assert (INSTANCE, "i") in kinds
    assert (SCHEMA, "s2") in kinds


def test_profile_excludes_tagged_group_without_dropping_it(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    xsd10_cases = [c for c in build_cases(catalog, XSD10) if c.group_name == "tagged"]
    assert xsd10_cases and all(not case.applicable for case in xsd10_cases)


def test_runner_classifies_verdicts(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = build_cases(catalog, XSD11)
    driver = ScriptedDriver(schema={"default": True}, instance={"i.xml": True})
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    by_name = {}
    for case in cases:
        for result in runner.run_case(case):
            by_name[case.test_id.split("/")[-1]] = result

    # schema "s" expected valid and the driver says valid
    assert by_name["s"].outcome is Outcome.PASS
    # instance "i" expected invalid but the driver says valid
    assert by_name["i"].outcome is Outcome.FAIL


def test_missing_schema_group_is_adapter_gap(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "noschema"]
    driver = ScriptedDriver(schema={"default": True}, instance={})
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]
    assert result.outcome is Outcome.ADAPTER_GAP
    assert driver.calls == []
    # Synthesis is opt-in; the default driver leaves the topology alone.
    assert driver.synthesize_missing_schema is False


def test_no_schema_group_is_synthesized_when_driver_opts_in(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "noschema"]
    driver = ScriptedDriver(schema={"default": True}, instance={}, synthesize_missing_schema=True)
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]

    # A real verdict, not an adapter gap.
    assert result.outcome is Outcome.PASS
    assert len(driver.calls) == 1
    kind, schema_path, instance_path = driver.calls[0]
    assert kind == "instance"
    assert instance_path.endswith("i2.xml")
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    assert 'name="x"' in schema_text
    assert 'type="xs:anyType"' in schema_text


def test_synthesized_schema_uses_the_instance_root_namespace(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "noschema-ns"]
    driver = ScriptedDriver(schema={"default": True}, instance={}, synthesize_missing_schema=True)
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    runner.run_case(cases[0])
    schema_text = Path(driver.calls[0][1]).read_text(encoding="utf-8")

    assert 'targetNamespace="urn:probe"' in schema_text
    assert 'name="root"' in schema_text
    assert 'type="xs:anyType"' in schema_text


def test_hinted_root_is_not_duplicated(tmp_path: Path) -> None:
    """A hinted schema that declares the root is not duplicated.

    Declaring the same global element twice would make the synthesized
    schema invalid; when the instance's own hint supplies the declaration,
    the wrapper omits it and lets the engine compose the hint.
    """
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "noschema-hinted"]
    driver = ScriptedDriver(schema={"default": True}, instance={}, synthesize_missing_schema=True)
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    runner.run_case(cases[0])
    schema_text = Path(driver.calls[0][1]).read_text(encoding="utf-8")

    assert 'type="xs:anyType"' not in schema_text
    assert "<xs:element" not in schema_text


def test_synthesis_does_not_mask_a_schema_phase_error(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "noschema"]
    driver = SchemaFailingDriver(
        schema={"default": True}, instance={}, synthesize_missing_schema=True
    )
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]

    # The driver observed a schema-phase error; the harness must not turn it
    # into a passing instance verdict by synthesizing a schema.
    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.detail == "group schema did not compile"


def test_synthesis_leaves_other_topologies_unchanged(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "ok"]
    driver = ScriptedDriver(schema={"default": True}, instance={}, synthesize_missing_schema=True)
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    runner.run_case(cases[0])

    # The supplied schema document is still the one handed to the driver.
    schema_path = driver.calls[-1][1]
    assert schema_path.endswith("s.xsd")
    assert "synthesized" not in schema_path


def test_not_applicable_case_never_calls_driver(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD10) if c.group_name == "tagged"]
    driver = ScriptedDriver(schema={"default": True}, instance={})
    runner = Runner(profile=XSD10, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]
    assert result.outcome is Outcome.NOT_APPLICABLE
    assert driver.calls == []


def test_synthesized_schema_still_validates_the_instance(tmp_path: Path) -> None:
    """The synthesized schema is no-opinion about content but is not a no-op.

    The instance declares an invalid built-in type through ``xsi:type``; a
    wrapper that merely made everything valid would report ``pass`` for the
    wrong reason. It must fail instead.
    """
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "noschema-typed"]
    driver = PyXSDDriver(timeout=10.0, synthesize_missing_schema=True)
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]

    assert result.outcome is Outcome.FAIL
    assert result.actual is False


def _schema_case(catalog, group_name: str):
    return next(
        case
        for case in build_cases(catalog, XSD11)
        if case.group_name == group_name and case.kind == SCHEMA
    )


def test_bundle_single_document_is_returned_unchanged(tmp_path: Path) -> None:
    """The single-schema path hands the driver the document itself, no wrapper."""
    write_corpus(tmp_path)
    document = DocumentRef(path=PurePosixPath("data/s.xsd"), raw_href="s.xsd")

    bundle = build_bundle(PurePosixPath(str(tmp_path)), (document,), tmp_path)

    assert Path(bundle) == tmp_path / "data" / "s.xsd"


def test_bundle_does_not_reload_a_redefined_document(tmp_path: Path) -> None:
    """A document redefined by another bundle member is not imported twice.

    Loading the principal (which already composes the redefine target) and
    then the target again declares its globals twice, so the composed schema
    fails with ``multiple-roots``.
    """
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    case = _schema_case(catalog, "redef")

    bundle = build_bundle(case.corpus_root, case.schema_documents, tmp_path)
    text = bundle.read_text(encoding="utf-8")

    assert "redefine.xsd" in text
    assert "redefined.xsd" not in text


def test_bundle_does_not_reload_an_imported_document(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    case = _schema_case(catalog, "imports")

    bundle = build_bundle(case.corpus_root, case.schema_documents, tmp_path)
    text = bundle.read_text(encoding="utf-8")

    assert "importer.xsd" in text
    assert "imported.xsd" not in text


def test_bundle_keeps_independent_documents(tmp_path: Path) -> None:
    """Documents that do not compose each other are all loaded."""
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    case = _schema_case(catalog, "independent")

    bundle = build_bundle(case.corpus_root, case.schema_documents, tmp_path)
    text = bundle.read_text(encoding="utf-8")

    assert "left.xsd" in text
    assert "right.xsd" in text


def test_redefine_bundle_yields_a_verdict_not_a_gap(tmp_path: Path) -> None:
    """The engine observes a real verdict, not "group schema did not compile"."""
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [
        case
        for case in build_cases(catalog, XSD11)
        if case.group_name == "redef" and case.kind == INSTANCE
    ]
    driver = PyXSDDriver(timeout=10.0)
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]

    assert result.outcome is not Outcome.ADAPTER_GAP
    assert result.outcome is not Outcome.ERROR


@pytest.mark.skipif(not corpus_available(), reason="xsdtests corpus not checked out")
def test_real_imported_role_bundle_loads_once(tmp_path: Path) -> None:
    """An ``MS-Additional`` bundle whose second document has role="imported".

    ``addB132`` lists the principal ``test66745_a.xsd`` and the
    ``role="imported"`` ``test66745_b.xsd`` it imports; the wrapper must load
    only the principal.
    """
    catalog = load_default_catalog()
    case = next(
        case
        for case in build_cases(catalog, XSD11)
        if case.test_id == "MS-Additional2006-07-15/addB132/addB132"
    )

    bundle = build_bundle(case.corpus_root, case.schema_documents, tmp_path)
    text = bundle.read_text(encoding="utf-8")

    assert "test66745_a.xsd" in text
    assert "test66745_b.xsd" not in text


@pytest.mark.skipif(not corpus_available(), reason="xsdtests corpus not checked out")
def test_real_redefine_bundle_yields_a_verdict(tmp_path: Path) -> None:
    """The ``defaultAttributesApply`` redefine bundle compiles and is decided.

    Its ``schemaTest`` lists the redefining document and the document it
    redefines; loading both declared ``root`` twice (``multiple-roots``), so
    the harness produced an adapter-gap.  The assembly now loads only the
    principal, and the engine returns the expected (invalid) verdict.
    """
    catalog = load_default_catalog()
    case = next(
        case
        for case in build_cases(catalog, XSD11)
        if case.test_id == "defaultAttributesApply/s3_4_2_4ii03/s3_4_2_4ii03i"
    )
    runner = Runner(profile=XSD11, driver=PyXSDDriver(timeout=30.0), workdir=tmp_path)

    result = runner.run_case(case)[0]

    assert result.outcome is Outcome.PASS
    assert result.detail != "group schema did not compile"


def test_bundle_failure_becomes_adapter_gap(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD11) if c.group_name == "ok"]
    driver = ScriptedDriver(schema={"default": True}, instance={})
    runner = Runner(profile=XSD11, driver=driver, workdir=tmp_path)

    # Remove the schema document so build_bundle raises HarnessError.
    (tmp_path / "data" / "s.xsd").unlink()
    results = runner.run_case(cases[0])
    assert results[0].outcome is Outcome.ADAPTER_GAP
