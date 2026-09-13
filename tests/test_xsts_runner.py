"""Case building and execution with a scripted driver."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from xsts.catalog import load_catalog
from xsts.outcomes import EngineResult, Outcome
from xsts.runner import INSTANCE, SCHEMA, Runner, build_cases
from xsts.selection import XSD10, XSD11

TS = "http://www.w3.org/XML/2004/xml-schema-test-suite/"
XLINK = "http://www.w3.org/1999/xlink"

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
</testSet>
"""


class ScriptedDriver:
    """Returns pre-scripted verdicts keyed by test id; records what was asked."""

    name = "scripted"

    def __init__(self, schema: dict[str, bool], instance: dict[str, bool]) -> None:
        self.schema = schema
        self.instance = instance
        self.calls: list[tuple[str, str]] = []

    def compile_schema(self, schema_path: Path) -> EngineResult:
        self.calls.append(("schema", str(schema_path)))
        return EngineResult(schema_valid=self.schema.get("default", True))

    def validate(self, schema_path: Path, instance_path: Path) -> EngineResult:
        self.calls.append(("instance", str(instance_path)))
        return EngineResult(
            schema_valid=self.schema.get("default", True),
            instance_valid=self.instance.get(instance_path.name, True),
        )


def write_corpus(root: Path) -> None:
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "suite.xml").write_text(SUITE)
    (root / "meta" / "a.testSet").write_text(A_SET)
    for name in ("s.xsd", "s2.xsd"):
        (root / "data" / name).write_text("<x/>")
    for name in ("i.xml", "i2.xml"):
        (root / "data" / name).write_text("<x/>")


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


def test_not_applicable_case_never_calls_driver(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    catalog = load_catalog(PurePosixPath(str(tmp_path)))
    cases = [c for c in build_cases(catalog, XSD10) if c.group_name == "tagged"]
    driver = ScriptedDriver(schema={"default": True}, instance={})
    runner = Runner(profile=XSD10, driver=driver, workdir=tmp_path)

    result = runner.run_case(cases[0])[0]
    assert result.outcome is Outcome.NOT_APPLICABLE
    assert driver.calls == []


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
