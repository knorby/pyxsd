"""Contract tests for the pinned RELAX NG conformance harness.

The corpus is a pair of upstream XML catalogs, not a directory tree.  These
tests pin both the synthetic catalog semantics (so the harness itself is
checked) and the exact shape of the pinned catalogs (so upstream drift is a
review event rather than a silently updated count).
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

import pytest
from tests.relaxng.baseline import (
    Baseline,
    BaselineDiff,
    BaselineError,
    Change,
    diff,
    dump,
)
from tests.relaxng.baseline import (
    load as load_baseline,
)
from tests.relaxng.baseline import (
    save as save_baseline,
)
from tests.relaxng.catalog import (
    CASE_DIRECTORY,
    SPECTEST_FILE,
    XSD_1_1_DATATYPES,
    XSDTEST_FILE,
    CatalogError,
    load_xml_syntax_catalog,
    load_xsd_datatype_catalog,
)
from tests.relaxng.dispositions import (
    CATEGORIES,
    Disposition,
    DispositionError,
    ReleaseViolation,
    load_dispositions,
    release_violations,
    validate_dispositions,
)
from tests.relaxng.model import (
    CASE_KINDS,
    EXCLUDED_PROFILES,
    INSTANCE_KINDS,
    PROFILE_COMPACT_SYNTAX,
    PROFILE_CORE,
    PROFILE_DTD_COMPATIBILITY,
    PROFILE_XSD_DATATYPE,
    PROFILES,
    REQUIRED_PROFILES,
    RUN_FULL_CORPUS_ENV,
    SCHEMA_KINDS,
    CorpusCase,
    Outcome,
    corpus_enabled,
    satisfies_expected,
)

from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import Element, Ref, Value

CORPUS = Path(__file__).resolve().parent / "relaxng" / "corpus"
CORPUS_FILES = (SPECTEST_FILE, XSDTEST_FILE, "LICENSE", "PROVENANCE.md", "SHA256SUMS")
CORPUS_AVAILABLE = all((CORPUS / name).is_file() for name in CORPUS_FILES)

# The research-established shape of the pinned XML-syntax catalog.  Changing
# these numbers requires reviewing the upstream catalog and PROVENANCE.md.
SPECTEST_SCHEMAS = 385
SPECTEST_INSTANCES = 580
SPECTEST_CORRECT = 172
SPECTEST_INCORRECT = 213
SPECTEST_VALID = 289
SPECTEST_INVALID = 291
SPECTEST_XSD_SCHEMAS = 9
SPECTEST_XSD_INSTANCES = 42
SPECTEST_RESOURCES = 31
SPECTEST_RESOURCE_DIRS = 7

# Derived from xsdtest.xml with the upstream xsdtest.xsl rules, excluding the
# two XSD 1.1-only datatypes that the XSD 1.0 adapter does not promise.
XSDTEST_SCHEMAS = 238
XSDTEST_INSTANCES = 1041
XSDTEST_DATATYPES = 42
XSDTEST_DATATYPE_NAMES = frozenset(
    {
        "dateTime",
        "anyURI",
        "integer",
        "duration",
        "time",
        "date",
        "gYearMonth",
        "gYear",
        "gMonthDay",
        "gDay",
        "gMonth",
        "boolean",
        "base64Binary",
        "hexBinary",
        "float",
        "double",
        "QName",
        "NOTATION",
        "decimal",
        "nonPositiveInteger",
        "nonNegativeInteger",
        "positiveInteger",
        "negativeInteger",
        "long",
        "int",
        "short",
        "byte",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
        "string",
        "normalizedString",
        "token",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "ID",
        "ENTITY",
        "ENTITIES",
    }
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def case(
    case_id: str,
    kind: str,
    profile: str = PROFILE_CORE,
    *,
    schema: Path | None = None,
    instance: Path | None = None,
) -> CorpusCase:
    return CorpusCase(
        id=case_id,
        kind=kind,  # type: ignore[arg-type]
        schema=schema or Path(f"{case_id}.rng"),
        instance=instance
        if instance is not None
        else (Path(f"{case_id}.xml") if kind in INSTANCE_KINDS else None),
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Case model and outcome taxonomy
# ---------------------------------------------------------------------------


def test_case_kind_partitions_are_complete_and_disjoint():
    assert set(CASE_KINDS) == {
        "correct-schema",
        "incorrect-schema",
        "valid-instance",
        "invalid-instance",
    }
    assert SCHEMA_KINDS.isdisjoint(INSTANCE_KINDS)
    assert set(CASE_KINDS) == SCHEMA_KINDS | INSTANCE_KINDS


def test_profiles_have_one_closed_vocabulary():
    assert {
        PROFILE_CORE,
        PROFILE_XSD_DATATYPE,
        PROFILE_COMPACT_SYNTAX,
        PROFILE_DTD_COMPATIBILITY,
    } == PROFILES
    assert {PROFILE_CORE, PROFILE_XSD_DATATYPE} == REQUIRED_PROFILES
    assert {PROFILE_COMPACT_SYNTAX, PROFILE_DTD_COMPATIBILITY} == EXCLUDED_PROFILES
    assert REQUIRED_PROFILES.isdisjoint(EXCLUDED_PROFILES)


def test_an_instance_case_requires_an_instance_path():
    with pytest.raises(ValueError, match="instance"):
        CorpusCase(
            id="x",
            kind="valid-instance",
            schema=Path("x.rng"),
            instance=None,
            profile=PROFILE_CORE,
        )
    with pytest.raises(ValueError, match="instance"):
        CorpusCase(
            id="x",
            kind="correct-schema",
            schema=Path("x.rng"),
            instance=Path("x.xml"),
            profile=PROFILE_CORE,
        )


@pytest.mark.parametrize(
    ("case_id", "kind", "profile", "message"),
    [
        ("", "correct-schema", PROFILE_CORE, "id"),
        ("x", "not-a-kind", PROFILE_CORE, "kind"),
        ("x", "correct-schema", "", "profile"),
        ("x", "correct-schema", "invented", "profile"),
    ],
)
def test_corpus_case_rejects_invalid_metadata(case_id, kind, profile, message):
    with pytest.raises(ValueError, match=message):
        CorpusCase(
            id=case_id,
            kind=kind,  # type: ignore[arg-type]
            schema=Path("x.rng"),
            instance=None,
            profile=profile,
        )


def test_corpus_case_properties_classify_scope():
    assert case("c", "correct-schema", PROFILE_CORE).is_required
    assert case("x", "correct-schema", PROFILE_XSD_DATATYPE).is_required
    assert case("k", "incorrect-schema", PROFILE_COMPACT_SYNTAX).is_excluded
    assert case("d", "invalid-instance", PROFILE_DTD_COMPATIBILITY).is_excluded
    assert not case("c", "correct-schema", PROFILE_CORE).is_excluded


def test_outcome_values_are_the_settled_taxonomy():
    assert {outcome.value for outcome in Outcome} == {
        "pass",
        "fail",
        "adapter-gap",
        "profile-excluded",
    }
    assert Outcome("pass") is Outcome.PASS


@pytest.mark.parametrize(
    "kind",
    ["correct-schema", "incorrect-schema", "valid-instance", "invalid-instance"],
)
@pytest.mark.parametrize("outcome", list(Outcome))
def test_only_a_matching_verdict_satisfies_an_expectation(kind, outcome):
    expectation = case("x", kind)
    assert satisfies_expected(expectation, outcome) is (outcome is Outcome.PASS)


@pytest.mark.parametrize("kind", ["incorrect-schema", "invalid-instance"])
def test_adapter_gap_never_satisfies_a_rejection(kind):
    rejection = case("negative", kind)
    assert not satisfies_expected(rejection, Outcome.ADAPTER_GAP)
    assert satisfies_expected(rejection, Outcome.PASS)


def test_full_corpus_execution_is_gated_by_the_documented_environment_value(monkeypatch):
    assert RUN_FULL_CORPUS_ENV == "PYXSD_RUN_RELAXNG"
    assert corpus_enabled({}) is False
    assert corpus_enabled({RUN_FULL_CORPUS_ENV: "0"}) is False
    assert corpus_enabled({RUN_FULL_CORPUS_ENV: ""}) is False
    assert corpus_enabled({RUN_FULL_CORPUS_ENV: "1"}) is True
    monkeypatch.delenv(RUN_FULL_CORPUS_ENV, raising=False)
    assert corpus_enabled() is False
    monkeypatch.setenv(RUN_FULL_CORPUS_ENV, "1")
    assert corpus_enabled() is True


# ---------------------------------------------------------------------------
# Synthetic XML-syntax catalog
# ---------------------------------------------------------------------------

SYNTHETIC_SPECTEST = """\
<?xml version="1.0"?>
<testSuite>
  <author>Harness Test</author>
  <testSuite>
    <documentation>Core cases</documentation>
    <testCase>
      <section>1</section>
      <correct>
        <element xmlns="http://relaxng.org/ns/structure/1.0" name="doc">
          <empty/>
        </element>
      </correct>
      <valid><doc/></valid>
      <invalid><other/></invalid>
    </testCase>
    <testCase>
      <incorrect>
        <element xmlns="http://relaxng.org/ns/structure/1.0">
          <empty/>
        </element>
      </incorrect>
    </testCase>
  </testSuite>
  <testSuite>
    <documentation>Datatype-sensitive cases</documentation>
    <requires datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
    <testCase>
      <correct>
        <element xmlns="http://relaxng.org/ns/structure/1.0" name="doc">
          <data type="string"
                datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
        </element>
      </correct>
      <valid><doc>x</doc></valid>
    </testCase>
  </testSuite>
  <testSuite>
    <testCase xmlns:e="http://www.example.com">
      <section>4</section>
      <correct>
        <element xmlns="http://relaxng.org/ns/structure/1.0" name="doc">
          <element name="e:foo"><empty/></element>
        </element>
      </correct>
      <resource name="part.rng">
        <element xmlns="http://relaxng.org/ns/structure/1.0" name="inner"><empty/></element>
      </resource>
      <dir name="sub">
        <resource name="nested.txt">hello &amp; goodbye</resource>
        <dir name="deeper">
          <resource name="leaf.txt">leaf</resource>
        </dir>
      </dir>
      <valid dtd="&lt;!DOCTYPE doc [&lt;!ENTITY x 'y'>]&gt;">
        <doc><e:foo/></doc>
      </valid>
    </testCase>
    <testCase>
      <requires datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      <incorrect>
        <element xmlns="http://relaxng.org/ns/structure/1.0" name="bad:name">
          <empty/>
        </element>
      </incorrect>
    </testCase>
  </testSuite>
</testSuite>
"""


@pytest.fixture
def synthetic_spectest(tmp_path: Path) -> Path:
    write(tmp_path / SPECTEST_FILE, SYNTHETIC_SPECTEST)
    return tmp_path


def test_synthetic_xml_syntax_catalog_has_stable_ids_and_kinds(synthetic_spectest):
    cases = load_xml_syntax_catalog(synthetic_spectest)
    assert [item.id for item in cases] == [
        "spectest-0001",
        "spectest-0001/valid-1",
        "spectest-0001/invalid-1",
        "spectest-0002",
        "spectest-0003",
        "spectest-0003/valid-1",
        "spectest-0004",
        "spectest-0004/valid-1",
        "spectest-0005",
    ]
    assert [item.kind for item in cases] == [
        "correct-schema",
        "valid-instance",
        "invalid-instance",
        "incorrect-schema",
        "correct-schema",
        "valid-instance",
        "correct-schema",
        "valid-instance",
        "incorrect-schema",
    ]
    assert [item.profile for item in cases] == [
        PROFILE_CORE,
        PROFILE_CORE,
        PROFILE_CORE,
        PROFILE_CORE,
        PROFILE_XSD_DATATYPE,
        PROFILE_XSD_DATATYPE,
        PROFILE_CORE,
        PROFILE_CORE,
        PROFILE_XSD_DATATYPE,
    ]


def test_synthetic_xml_syntax_catalog_writes_resolved_case_files(synthetic_spectest):
    cases = {item.id: item for item in load_xml_syntax_catalog(synthetic_spectest)}
    root = synthetic_spectest / CASE_DIRECTORY / "spectest"

    schema = cases["spectest-0001"]
    assert schema.schema == root / "0001" / "c.rng"
    assert schema.instance is None
    assert "http://relaxng.org/ns/structure/1.0" in schema.schema.read_text(encoding="utf-8")

    incorrect = cases["spectest-0002"]
    assert incorrect.schema == root / "0002" / "i.rng"

    assert cases["spectest-0001/valid-1"].instance == root / "0001" / "1.v.xml"
    assert cases["spectest-0001/invalid-1"].instance == root / "0001" / "1.i.xml"
    assert cases["spectest-0001/valid-1"].schema == schema.schema

    assert (root / "0004" / "part.rng").is_file()
    assert (root / "0004" / "sub" / "nested.txt").read_text(encoding="utf-8") == ("hello & goodbye")
    assert (root / "0004" / "sub" / "deeper" / "leaf.txt").read_text(encoding="utf-8") == "leaf"

    valid = cases["spectest-0004/valid-1"].instance
    assert valid is not None
    valid_text = valid.read_text(encoding="utf-8")
    assert valid_text.startswith("<!DOCTYPE doc [")
    assert "<!ENTITY x 'y'>" in valid_text
    # Prefixes declared on an ancestor of the embedded fragment stay usable.
    assert 'xmlns:e="http://www.example.com"' in valid_text
    assert "<e:foo" in valid_text

    schema_text = cases["spectest-0004"].schema.read_text(encoding="utf-8")
    assert 'xmlns:e="http://www.example.com"' in schema_text
    assert 'name="e:foo"' in schema_text


def test_synthetic_xml_syntax_catalog_round_trips_deterministically(synthetic_spectest):
    first = load_xml_syntax_catalog(synthetic_spectest)
    snapshot = {
        path: path.read_bytes()
        for path in (synthetic_spectest / CASE_DIRECTORY).rglob("*")
        if path.is_file()
    }
    first[0].schema.write_text("stale", encoding="utf-8")
    second = load_xml_syntax_catalog(synthetic_spectest)
    assert first == second
    assert snapshot == {
        path: path.read_bytes()
        for path in (synthetic_spectest / CASE_DIRECTORY).rglob("*")
        if path.is_file()
    }


def test_xml_syntax_catalog_requires_the_pinned_file(tmp_path):
    with pytest.raises(CatalogError, match=SPECTEST_FILE):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_reports_malformed_xml(tmp_path):
    write(tmp_path / SPECTEST_FILE, "<testSuite><testCase>")
    with pytest.raises(CatalogError, match="parse"):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_rejects_a_foreign_root(tmp_path):
    write(tmp_path / SPECTEST_FILE, "<catalog/>")
    with pytest.raises(CatalogError, match="testSuite"):
        load_xml_syntax_catalog(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "<testCase><section>1</section></testCase>",
        (
            "<testCase><correct><element xmlns='http://relaxng.org/ns/structure/1.0'/>"
            "</correct><incorrect><element xmlns='http://relaxng.org/ns/structure/1.0'/>"
            "</incorrect></testCase>"
        ),
        "<testCase><correct/><correct/></testCase>",
        "<testCase><unknown/></testCase>",
    ],
)
def test_xml_syntax_catalog_rejects_malformed_cases(tmp_path, body):
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError):
        load_xml_syntax_catalog(tmp_path)


@pytest.mark.parametrize(
    "container",
    [
        "<correct/>",
        "<correct><element xmlns='http://relaxng.org/ns/structure/1.0'/>"
        "<element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>",
    ],
)
def test_xml_syntax_catalog_requires_one_schema_element(tmp_path, container):
    write(
        tmp_path / SPECTEST_FILE,
        f"<testSuite><testCase>{container}</testCase></testSuite>",
    )
    with pytest.raises(CatalogError, match="schema"):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_requires_an_instance_element(tmp_path):
    body = (
        "<testCase><correct><element xmlns='http://relaxng.org/ns/structure/1.0'/>"
        "</correct><valid/></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match="instance"):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_rejects_instances_on_an_incorrect_schema(tmp_path):
    body = (
        "<testCase><incorrect><element xmlns='http://relaxng.org/ns/structure/1.0'/>"
        "</incorrect><valid><doc/></valid></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match="incorrect"):
        load_xml_syntax_catalog(tmp_path)


@pytest.mark.parametrize(
    "requires",
    [
        "<requires/>",
        "<requires datatypeLibrary='urn:not-supported'/>",
    ],
)
def test_xml_syntax_catalog_rejects_unsupported_requirements(tmp_path, requires):
    body = (
        "<testCase><requires datatypeLibrary='http://www.w3.org/2001/XMLSchema-datatypes'/>"
        f"{requires}<correct><element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>"
        "</testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite><testSuite>{body}</testSuite></testSuite>")
    with pytest.raises(CatalogError, match="requires"):
        load_xml_syntax_catalog(tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "a/b",
        "back\\slash",
        "nön-ascii",
        "a b",
        ".",
        "..",
    ],
)
def test_xml_syntax_catalog_rejects_unsafe_resource_paths(tmp_path, name):
    body = (
        "<testCase><correct><element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>"
        f"<resource name='{name}'>x</resource></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match=r"resource|path"):
        load_xml_syntax_catalog(tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_xml_syntax_catalog_rejects_duplicate_resource_paths(tmp_path):
    body = (
        "<testCase><correct><element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>"
        "<resource name='x'>one</resource><resource name='x'>two</resource></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match="duplicate"):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_reports_an_unwritable_case_path(tmp_path):
    body = (
        "<testCase><correct><element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>"
        "<valid><doc/></valid></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    (tmp_path / CASE_DIRECTORY / "spectest" / "0001" / "c.rng").mkdir(parents=True)
    with pytest.raises(CatalogError, match="cannot write"):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_treats_a_blank_doctype_as_absent(tmp_path):
    body = (
        "<testCase><correct dtd=''>"
        "<element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>"
        "<valid dtd=''><doc/></valid></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    cases = {item.id: item for item in load_xml_syntax_catalog(tmp_path)}
    assert "DOCTYPE" not in cases["spectest-0001"].schema.read_text(encoding="utf-8")
    instance = cases["spectest-0001/valid-1"].instance
    assert instance is not None
    assert "DOCTYPE" not in instance.read_text(encoding="utf-8")


@pytest.mark.parametrize("tag", ["resource", "dir"])
def test_xml_syntax_catalog_requires_resource_and_dir_names(tmp_path, tag):
    body = (
        "<testCase><correct><element xmlns='http://relaxng.org/ns/structure/1.0'/></correct>"
        f"<{tag}>x</{tag}></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    with pytest.raises(CatalogError, match=tag):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_rejects_unknown_suite_children(tmp_path):
    write(
        tmp_path / SPECTEST_FILE,
        "<testSuite><bogus/></testSuite>",
    )
    with pytest.raises(CatalogError, match="bogus"):
        load_xml_syntax_catalog(tmp_path)


def test_xml_syntax_catalog_does_not_inject_an_empty_default_namespace(tmp_path):
    body = (
        "<testCase xmlns='' xmlns:e='http://www.example.com'>"
        "<correct><element xmlns='http://relaxng.org/ns/structure/1.0' name='doc'>"
        "<element name='e:foo'><empty/></element></element></correct>"
        "<valid><doc><e:foo/></doc></valid></testCase>"
    )
    write(tmp_path / SPECTEST_FILE, f"<testSuite>{body}</testSuite>")
    cases = {item.id: item for item in load_xml_syntax_catalog(tmp_path)}
    schema = cases["spectest-0001"].schema.read_text(encoding="utf-8")
    assert 'xmlns:e="http://www.example.com"' in schema
    assert 'xmlns=""' not in schema


def test_xml_syntax_catalog_materializes_inherited_default_namespace_undeclaration(tmp_path):
    write(
        tmp_path / SPECTEST_FILE,
        """<testSuite xmlns="urn:outer" xmlns:p="urn:prefixed"
            xmlns:r="http://relaxng.org/ns/structure/1.0">
          <testSuite xmlns="">
            <testCase>
              <correct><r:element name="doc"><r:value>foo</r:value></r:element></correct>
              <valid><doc><p:child/></doc></valid>
            </testCase>
          </testSuite>
        </testSuite>""",
    )
    cases = {item.id: item for item in load_xml_syntax_catalog(tmp_path)}
    case = cases["spectest-0001/valid-1"]
    schema_xml = case.schema.read_text(encoding="utf-8")
    instance_xml = case.instance.read_text(encoding="utf-8")
    assert 'xmlns="urn:outer"' not in schema_xml + instance_xml
    assert 'xmlns:p="urn:prefixed"' in schema_xml
    assert ET.fromstring(schema_xml).tag == "{http://relaxng.org/ns/structure/1.0}element"
    instance = ET.fromstring(instance_xml)
    assert instance.tag == "doc"
    assert instance[0].tag == "{urn:prefixed}child"
    schema = RelaxNGSchema.compile(case.schema)
    schema.require_valid()
    start = schema.grammar.start
    while isinstance(start, Ref):
        start = start.target
    assert isinstance(start, Element)
    assert start.name_class == Name(None, "doc")
    assert schema.namespace_context.root_bindings.get("", "") == ""


# ---------------------------------------------------------------------------
# Synthetic XSD datatype catalog
# ---------------------------------------------------------------------------

SYNTHETIC_XSDTEST = """\
<xsdtest>
  <datatype name="integer">
    <param name="pattern">[0-9]+</param>
    <valid>10</valid>
    <invalid>1.0</invalid>
    <equiv>
      <class><value>1</value><value>+1</value><value>01</value></class>
      <class><value>2</value></class>
    </equiv>
    <length value="0"> </length>
    <length value="2">12</length>
    <lessThan><value>1</value><value>2</value></lessThan>
    <incomparable><value>a</value><value>b</value></incomparable>
  </datatype>
  <datatype name="QName">
    <valid xmlns:x="http://www.example.com">x:foo</valid>
    <invalid xmlns:x="http://www.example.com">x::foo</invalid>
  </datatype>
  <datatype name="ENTITY">
    <valid internalSubset="&lt;!ENTITY foo SYSTEM 'whatever' NDATA jpeg>">foo</valid>
  </datatype>
  <datatype name="untypedAtomic"><valid>x</valid></datatype>
  <datatype name="anyAtomicType"><valid>x</valid></datatype>
</xsdtest>
"""


@pytest.fixture
def synthetic_xsdtest(tmp_path: Path) -> Path:
    write(tmp_path / XSDTEST_FILE, SYNTHETIC_XSDTEST)
    return tmp_path


def test_xsd_catalog_data_and_value_paths_preserve_default_namespace_undeclaration(tmp_path):
    write(
        tmp_path / XSDTEST_FILE,
        """<xsdtest xmlns="urn:outer" xmlns:p="urn:prefixed">
          <datatype name="QName" xmlns="">
            <valid>local</valid>
            <equiv><class><value>local</value><value>local</value></class></equiv>
          </datatype>
        </xsdtest>""",
    )
    cases = {item.id: item for item in load_xsd_datatype_catalog(tmp_path)}
    for number in (1, 2):
        case = cases[f"xsdtest-QName-{number:02d}/valid-1"]
        instance_xml = case.instance.read_text(encoding="utf-8")
        assert 'xmlns="urn:outer"' not in instance_xml
        assert 'xmlns:p="urn:prefixed"' in instance_xml
        assert ET.fromstring(instance_xml).tag == "doc"
        schema_xml = case.schema.read_text(encoding="utf-8")
        assert 'xmlns="urn:outer"' not in schema_xml
        schema = RelaxNGSchema.compile(case.schema)
        schema.require_valid()
        start = schema.grammar.start
        while isinstance(start, Ref):
            start = start.target
        assert isinstance(start, Element)
        assert start.name_class == Name(None, "doc")
        if number == 2:
            assert isinstance(start.pattern, Value)
            datatype = start.pattern.datatype
            assert datatype.context.namespaces.get("", "") == ""
            decoded = datatype.decode("local", datatype.context)
            assert decoded.to_expanded_name(strict=True) == "local"


def test_synthetic_xsd_datatype_catalog_expands_facets_and_skips_xsd11(
    synthetic_xsdtest,
):
    cases = load_xsd_datatype_catalog(synthetic_xsdtest)
    schemas = [item for item in cases if item.kind == "correct-schema"]
    instances = [item for item in cases if item.kind != "correct-schema"]

    assert len(schemas) == 14
    assert len(instances) == 24
    assert {item.profile for item in cases} == {PROFILE_XSD_DATATYPE}
    assert [item.id for item in schemas] == [
        "xsdtest-integer-01",
        "xsdtest-integer-02",
        "xsdtest-integer-03",
        "xsdtest-integer-04",
        "xsdtest-integer-05",
        "xsdtest-integer-06",
        "xsdtest-integer-07",
        "xsdtest-integer-08",
        "xsdtest-integer-09",
        "xsdtest-integer-10",
        "xsdtest-integer-11",
        "xsdtest-integer-12",
        "xsdtest-QName-01",
        "xsdtest-ENTITY-01",
    ]
    assert not any("untypedAtomic" in item.id for item in cases)
    assert not any("anyAtomicType" in item.id for item in cases)

    by_id = {item.id: item for item in cases}
    base = by_id["xsdtest-integer-01"]
    base_text = base.schema.read_text(encoding="utf-8")
    assert 'datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"' in base_text
    assert '<param name="pattern">[0-9]+</param>' in base_text
    assert by_id["xsdtest-integer-01/valid-1"].instance is not None
    assert by_id["xsdtest-integer-01/valid-1"].instance.read_text(encoding="utf-8") == (
        "<doc>10</doc>"
    )
    assert by_id["xsdtest-integer-01/invalid-1"].instance.read_text(encoding="utf-8") == (
        "<doc>1.0</doc>"
    )

    class_one = by_id["xsdtest-integer-02"]
    class_one_text = class_one.schema.read_text(encoding="utf-8")
    assert '<value type="integer">1</value>' in class_one_text
    assert by_id["xsdtest-integer-02/valid-1"].instance.read_text(encoding="utf-8") == (
        "<doc>+1</doc>"
    )
    assert by_id["xsdtest-integer-02/valid-2"].instance.read_text(encoding="utf-8") == (
        "<doc>01</doc>"
    )
    assert by_id["xsdtest-integer-02/invalid-1"].instance.read_text(encoding="utf-8") == (
        "<doc>2</doc>"
    )
    assert by_id["xsdtest-integer-03/invalid-3"].instance.read_text(encoding="utf-8") == (
        "<doc>01</doc>"
    )

    assert '<param name="length">0</param>' in by_id["xsdtest-integer-04"].schema.read_text(
        encoding="utf-8"
    )
    assert '<param name="length">1</param>' in by_id["xsdtest-integer-05"].schema.read_text(
        encoding="utf-8"
    )
    assert '<param name="length">2</param>' in by_id["xsdtest-integer-06"].schema.read_text(
        encoding="utf-8"
    )
    assert '<param name="length">3</param>' in by_id["xsdtest-integer-07"].schema.read_text(
        encoding="utf-8"
    )
    assert '<param name="minExclusive">1</param>' in by_id["xsdtest-integer-09"].schema.read_text(
        encoding="utf-8"
    )

    qname = by_id["xsdtest-QName-01/valid-1"].instance.read_text(encoding="utf-8")
    assert 'xmlns:x="http://www.example.com"' in qname
    assert qname.endswith(">x:foo</doc>")

    entity = by_id["xsdtest-ENTITY-01/valid-1"].instance.read_text(encoding="utf-8")
    assert entity.startswith("<!DOCTYPE doc [")
    assert "<!ENTITY foo SYSTEM 'whatever' NDATA jpeg>" in entity
    assert entity.endswith("<doc>foo</doc>")


def test_synthetic_xsd_datatype_catalog_round_trips_deterministically(synthetic_xsdtest):
    first = load_xsd_datatype_catalog(synthetic_xsdtest)
    second = load_xsd_datatype_catalog(synthetic_xsdtest)
    assert first == second
    assert len({item.id for item in first}) == len(first)


def test_xsd_datatype_catalog_requires_the_pinned_file(tmp_path):
    with pytest.raises(CatalogError, match=XSDTEST_FILE):
        load_xsd_datatype_catalog(tmp_path)


def test_xsd_datatype_catalog_rejects_a_foreign_root(tmp_path):
    write(tmp_path / XSDTEST_FILE, "<testSuite/>")
    with pytest.raises(CatalogError, match="xsdtest"):
        load_xsd_datatype_catalog(tmp_path)


def test_xsd_datatype_catalog_rejects_malformed_xml(tmp_path):
    write(tmp_path / XSDTEST_FILE, "<xsdtest><datatype>")
    with pytest.raises(CatalogError, match="parse"):
        load_xsd_datatype_catalog(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "<datatype><valid>x</valid></datatype>",
        "<datatype name='integer'><unknown/></datatype>",
        "<datatype name='integer'><length>1</length></datatype>",
        "<datatype name='integer'><length value='x'>1</length></datatype>",
        "<datatype name='integer'><length value='-1'>1</length></datatype>",
        "<datatype name='integer'><lessThan><value>1</value></lessThan></datatype>",
        "<datatype name='integer'><lessThan><bad/><value>1</value></lessThan></datatype>",
        "<datatype name='integer'><equiv/></datatype>",
        "<datatype name='integer'><equiv><bad/></equiv></datatype>",
        "<datatype name='integer'><equiv><class/></equiv></datatype>",
        "<unknown/>",
    ],
)
def test_xsd_datatype_catalog_rejects_malformed_entries(tmp_path, body):
    write(tmp_path / XSDTEST_FILE, f"<xsdtest>{body}</xsdtest>")
    with pytest.raises(CatalogError):
        load_xsd_datatype_catalog(tmp_path)


def test_xsd_datatype_catalog_rejects_a_blank_datatype_name(tmp_path):
    write(
        tmp_path / XSDTEST_FILE,
        "<xsdtest><datatype name=''><valid>x</valid></datatype></xsdtest>",
    )
    with pytest.raises(CatalogError, match="name"):
        load_xsd_datatype_catalog(tmp_path)


def test_xsd_datatype_catalog_treats_a_blank_internal_subset_as_absent(tmp_path):
    write(
        tmp_path / XSDTEST_FILE,
        "<xsdtest><datatype name='token'><valid internalSubset=''>x</valid></datatype></xsdtest>",
    )
    cases = {item.id: item for item in load_xsd_datatype_catalog(tmp_path)}
    instance = cases["xsdtest-token-01/valid-1"].instance
    assert instance is not None
    assert "DOCTYPE" not in instance.read_text(encoding="utf-8")


def test_xsd_datatype_catalog_uses_the_string_value_of_nested_values(tmp_path):
    write(
        tmp_path / XSDTEST_FILE,
        "<xsdtest><datatype name='token'><valid><b>x</b><!--ignored--></valid>"
        "</datatype></xsdtest>",
    )
    cases = {item.id: item for item in load_xsd_datatype_catalog(tmp_path)}
    instance = cases["xsdtest-token-01/valid-1"].instance
    assert instance is not None
    assert instance.read_text(encoding="utf-8") == "<doc>x</doc>"


def test_xsd_datatype_catalog_rejects_duplicate_datatype_names(tmp_path):
    write(
        tmp_path / XSDTEST_FILE,
        "<xsdtest><datatype name='integer'><valid>1</valid></datatype>"
        "<datatype name='integer'><valid>2</valid></datatype></xsdtest>",
    )
    with pytest.raises(CatalogError, match="duplicate"):
        load_xsd_datatype_catalog(tmp_path)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def test_baseline_dump_is_sorted_and_round_trips(tmp_path):
    text = dump(
        {"zeta": "pass", "alpha": "fail"},
        {"profile": "core", "count": 2},
    )
    assert text == (
        '[meta]\ncount = 2\nprofile = "core"\n\n[results]\n"alpha" = "fail"\n"zeta" = "pass"\n'
    )
    path = tmp_path / "baseline.toml"
    save_baseline(path, {"zeta": "pass", "alpha": "fail"}, {"profile": "core", "count": 2})
    baseline = load_baseline(path)
    assert baseline == Baseline(
        meta={"profile": "core", "count": 2},
        results={"alpha": "fail", "zeta": "pass"},
    )
    assert dump(baseline.results, baseline.meta) == text


def test_baseline_missing_file_is_empty(tmp_path):
    assert load_baseline(tmp_path / "missing.toml") == Baseline()


def test_baseline_load_accepts_results_without_metadata(tmp_path):
    path = write(tmp_path / "baseline.toml", '[results]\n"x" = "pass"\n')
    assert load_baseline(path) == Baseline(results={"x": "pass"})


@pytest.mark.parametrize(
    "text",
    [
        '[results]\n"x" = "pass"\n"x" = "fail"\n',
        '[results]\n"x" = "skipped"\n',
        '[results]\n"x" = 1\n',
        "[other]\nx = 1\n",
        "[meta]\nx = [1]\n",
        "meta = 1\n",
        "results = 1\n",
        "not toml",
    ],
)
def test_baseline_load_rejects_malformed_or_unknown_data(tmp_path, text):
    path = write(tmp_path / "baseline.toml", text)
    with pytest.raises(BaselineError):
        load_baseline(path)


def test_baseline_dump_rejects_values_that_would_not_round_trip():
    with pytest.raises(BaselineError, match="scalar"):
        dump({"x": "pass"}, {"bad": [1]})
    with pytest.raises(BaselineError, match="bare key"):
        dump({"x": "pass"}, {"bad key": 1})
    with pytest.raises(BaselineError, match="case ids"):
        dump({" ": "pass"})
    with pytest.raises(BaselineError, match="outcome"):
        dump({"x": "skipped"})


def test_baseline_load_reports_an_unreadable_file(tmp_path, monkeypatch):
    path = write(tmp_path / "baseline.toml", "[results]\n")
    original = Path.read_text

    def fail(self, *args, **kwargs):
        if self == path:
            raise OSError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(BaselineError, match="cannot read"):
        load_baseline(path)


def test_baseline_diff_classifies_every_change():
    baseline = Baseline(
        results={
            "regress": "pass",
            "improve": "fail",
            "changed": "fail",
            "removed": "fail",
        }
    )
    comparison = diff(
        baseline,
        {
            "regress": "fail",
            "improve": "pass",
            "changed": "adapter-gap",
            "new": "pass",
        },
    )
    assert isinstance(comparison, BaselineDiff)
    assert comparison
    assert [change.kind for change in comparison.changes] == [
        "changed",
        "improvement",
        "new",
        "regression",
        "removed",
    ]
    assert [change.key for change in comparison.changes] == [
        "changed",
        "improve",
        "new",
        "regress",
        "removed",
    ]
    assert [change.key for change in comparison.regressions] == ["regress"]
    assert [change.key for change in comparison.improvements] == ["improve"]
    assert Change(key="x", previous="pass", current="fail").kind == "regression"
    assert not diff(baseline, dict(baseline.results))


# ---------------------------------------------------------------------------
# Dispositions and release rules
# ---------------------------------------------------------------------------


def core_case(case_id: str = "core") -> CorpusCase:
    return case(case_id, "correct-schema", PROFILE_CORE)


def xsd_case(case_id: str = "xsd") -> CorpusCase:
    return case(case_id, "valid-instance", PROFILE_XSD_DATATYPE)


def compact_case(case_id: str = "compact") -> CorpusCase:
    return case(case_id, "correct-schema", PROFILE_COMPACT_SYNTAX)


def dtd_case(case_id: str = "dtd") -> CorpusCase:
    return case(case_id, "incorrect-schema", PROFILE_DTD_COMPATIBILITY)


def disposition(
    entry_id: str,
    category: str,
    cases: tuple[str, ...],
    *,
    feature: str | None = None,
) -> Disposition:
    return Disposition(
        id=entry_id,
        title=entry_id,
        category=category,
        cases=cases,
        rationale="because",
        feature=feature,
    )


def test_disposition_categories_are_the_profile_vocabulary():
    assert CATEGORIES == PROFILES


def test_dispositions_load_shape_and_defaults(tmp_path):
    path = write(
        tmp_path / "dispositions.toml",
        """
[[dispositions]]
id = "compact"
title = "Compact syntax"
category = "compact-syntax"
feature = "compact-syntax"
cases = ["c1", "c2"]
rationale = "The compact frontend is outside this profile."
links = ["https://relaxng.org/compact.html"]

[[dispositions]]
id = "core-note"
title = "Core"
category = "core"
cases = ["c3"]
rationale = "Required."
""",
    )
    assert load_dispositions(path) == (
        Disposition(
            id="compact",
            title="Compact syntax",
            category=PROFILE_COMPACT_SYNTAX,
            cases=("c1", "c2"),
            rationale="The compact frontend is outside this profile.",
            feature=PROFILE_COMPACT_SYNTAX,
            links=("https://relaxng.org/compact.html",),
        ),
        Disposition(
            id="core-note",
            title="Core",
            category=PROFILE_CORE,
            cases=("c3",),
            rationale="Required.",
        ),
    )


def test_an_empty_dispositions_file_is_allowed(tmp_path):
    assert load_dispositions(write(tmp_path / "d.toml", "")) == ()


def test_disposition_lists_default_to_empty(tmp_path):
    path = write(
        tmp_path / "d.toml",
        "[[dispositions]]\nid = 'x'\ntitle = 't'\ncategory = 'core'\nrationale = 'r'\n",
    )
    assert load_dispositions(path) == (
        Disposition(
            id="x",
            title="t",
            category=PROFILE_CORE,
            cases=(),
            rationale="r",
        ),
    )


@pytest.mark.parametrize(
    "text",
    [
        "[[dispositions]]\nid = 'x'\n",
        "[[dispositions]]\nid = 'x'\ntitle = 't'\ncategory = 'core'\n"
        "cases = ['c']\nrationale = 'r'\nunknown = 1\n",
        "[[dispositions]]\nid = 'x'\ntitle = 't'\ncategory = 'compact-syntax'\n"
        "cases = ['c']\nrationale = 'r'\nfeature = 1\n",
        "[[dispositions]]\nid = 'x'\ntitle = 't'\ncategory = 'core'\n"
        "cases = [1]\nrationale = 'r'\n",
        "dispositions = [1]\n",
        "[other]\nx = 1\n",
        "dispositions = 1\n",
        "not toml",
    ],
)
def test_dispositions_load_rejects_malformed_shape(tmp_path, text):
    with pytest.raises(DispositionError):
        load_dispositions(write(tmp_path / "d.toml", text))


def test_dispositions_load_reports_an_unreadable_file(tmp_path, monkeypatch):
    path = write(tmp_path / "d.toml", "")
    original = Path.read_text

    def fail(self, *args, **kwargs):
        if self == path:
            raise OSError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(DispositionError, match="cannot read"):
        load_dispositions(path)


def test_dispositions_validate_required_and_excluded_entries():
    cases = {
        "core": core_case(),
        "xsd": xsd_case(),
        "compact": compact_case(),
        "dtd": dtd_case(),
    }
    valid = (
        disposition("core-note", PROFILE_CORE, ("core",)),
        disposition("xsd-note", PROFILE_XSD_DATATYPE, ("xsd",)),
        disposition(
            "compact-note",
            PROFILE_COMPACT_SYNTAX,
            ("compact",),
            feature=PROFILE_COMPACT_SYNTAX,
        ),
        disposition(
            "dtd-note",
            PROFILE_DTD_COMPATIBILITY,
            ("dtd",),
            feature=PROFILE_DTD_COMPATIBILITY,
        ),
    )
    validate_dispositions(valid, cases)


def test_every_excluded_case_needs_an_explicit_feature_disposition():
    cases = {"compact": compact_case()}
    with pytest.raises(DispositionError, match="compact"):
        validate_dispositions((), cases)


def test_core_cases_cannot_be_reclassified_as_excluded():
    cases = {"core": core_case()}
    with pytest.raises(DispositionError, match="profile"):
        validate_dispositions(
            (
                disposition(
                    "waive",
                    PROFILE_COMPACT_SYNTAX,
                    ("core",),
                    feature=PROFILE_COMPACT_SYNTAX,
                ),
            ),
            cases,
        )


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (disposition("ex", PROFILE_COMPACT_SYNTAX, ("compact",)), "feature"),
        (
            disposition(
                "ex",
                PROFILE_COMPACT_SYNTAX,
                ("compact",),
                feature=PROFILE_DTD_COMPATIBILITY,
            ),
            "feature",
        ),
        (
            disposition("core", PROFILE_CORE, ("core",), feature=PROFILE_COMPACT_SYNTAX),
            "feature",
        ),
        (disposition("", PROFILE_CORE, ("core",)), "id"),
        (disposition("dup", "invented", ("core",)), "category"),
        (disposition("dup", PROFILE_CORE, ()), "cases"),
        (
            Disposition(
                id="blank",
                title="title",
                category=PROFILE_CORE,
                cases=("core",),
                rationale=" ",
            ),
            "rationale",
        ),
    ],
)
def test_disposition_validation_rejects_invalid_entries(entry, message):
    cases = {"core": core_case(), "compact": compact_case()}
    with pytest.raises(DispositionError, match=message):
        validate_dispositions((entry,), cases)


def test_disposition_validation_rejects_unknown_and_duplicate_claims():
    cases = {"core": core_case()}
    with pytest.raises(DispositionError, match="unknown"):
        validate_dispositions((disposition("x", PROFILE_CORE, ("missing",)),), cases)
    with pytest.raises(DispositionError, match="duplicate"):
        validate_dispositions(
            (
                disposition("a", PROFILE_CORE, ("core",)),
                disposition("b", PROFILE_CORE, ("core",)),
            ),
            cases,
        )
    with pytest.raises(DispositionError, match="duplicate"):
        validate_dispositions(
            (disposition("a", PROFILE_CORE, ("core", "core")),),
            cases,
        )
    with pytest.raises(DispositionError, match="duplicate"):
        validate_dispositions(
            (
                disposition("a", PROFILE_CORE, ("core",)),
                disposition("a", PROFILE_CORE, ()),
            ),
            cases,
        )


def test_release_requires_every_required_case_to_pass():
    cases = {
        "core-schema": core_case("core-schema"),
        "xsd-instance": xsd_case("xsd-instance"),
        "compact": compact_case(),
    }
    dispositions = (
        disposition(
            "compact-note",
            PROFILE_COMPACT_SYNTAX,
            ("compact",),
            feature=PROFILE_COMPACT_SYNTAX,
        ),
    )
    baseline = Baseline(
        results={
            "core-schema": "pass",
            "xsd-instance": "pass",
            "compact": "profile-excluded",
        }
    )
    assert release_violations(cases, baseline, dispositions) == ()


@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        ("fail", "incorrect-schema"),
        ("adapter-gap", "incorrect-schema"),
        ("adapter-gap", "invalid-instance"),
        ("profile-excluded", "correct-schema"),
    ],
)
def test_release_rejects_non_pass_outcomes_for_required_cases(outcome, kind):
    cases = {"core": case("core", kind)}
    baseline = Baseline(results={"core": outcome})
    violations = release_violations(cases, baseline, ())
    assert len(violations) == 1
    violation = violations[0]
    assert isinstance(violation, ReleaseViolation)
    assert violation.case_id == "core"
    assert violation.outcome == outcome
    assert violation.category == PROFILE_CORE


def test_adapter_gap_cannot_satisfy_an_expected_rejection():
    cases = {"bad": case("bad", "incorrect-schema")}
    violation = release_violations(cases, Baseline(results={"bad": "adapter-gap"}), ())
    assert len(violation) == 1
    assert not satisfies_expected(cases["bad"], Outcome.ADAPTER_GAP)


def test_release_rejects_missing_and_unknown_baseline_entries():
    cases = {"core": core_case()}
    missing = release_violations(cases, Baseline(), ())
    assert [item.case_id for item in missing] == ["core"]
    assert missing[0].outcome is None

    unknown = release_violations(
        cases,
        Baseline(results={"core": "pass", "stale": "pass"}),
        (),
    )
    assert [item.case_id for item in unknown] == ["stale"]


def test_release_accepts_an_excluded_case_that_was_not_run():
    cases = {"dtd": dtd_case()}
    dispositions = (
        disposition(
            "dtd-note",
            PROFILE_DTD_COMPATIBILITY,
            ("dtd",),
            feature=PROFILE_DTD_COMPATIBILITY,
        ),
    )
    assert release_violations(cases, Baseline(), dispositions) == ()
    assert (
        release_violations(
            cases,
            Baseline(results={"dtd": "profile-excluded"}),
            dispositions,
        )
        == ()
    )
    assert release_violations(cases, Baseline(results={"dtd": "pass"}), dispositions) == ()
    # Excluded cases are outside the release gate, including a recorded failure.
    assert release_violations(cases, Baseline(results={"dtd": "fail"}), dispositions) == ()


def test_release_refuses_invalid_dispositions():
    cases = {"core": core_case()}
    with pytest.raises(DispositionError):
        release_violations(
            cases,
            Baseline(results={"core": "pass"}),
            (
                disposition(
                    "waive",
                    PROFILE_COMPACT_SYNTAX,
                    ("core",),
                    feature=PROFILE_COMPACT_SYNTAX,
                ),
            ),
        )


# ---------------------------------------------------------------------------
# The pinned corpus itself
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def copied_corpus(tmp_path_factory) -> Path:
    if not CORPUS_AVAILABLE:
        pytest.skip("the pinned corpus is checkout-only")
    root = tmp_path_factory.mktemp("relaxng-corpus")
    for name in (SPECTEST_FILE, XSDTEST_FILE):
        (root / name).write_bytes((CORPUS / name).read_bytes())
    return root


@pytest.fixture(scope="session")
def spectest_cases(copied_corpus: Path) -> tuple[CorpusCase, ...]:
    return load_xml_syntax_catalog(copied_corpus)


@pytest.fixture(scope="session")
def xsdtest_cases(copied_corpus: Path) -> tuple[CorpusCase, ...]:
    return load_xsd_datatype_catalog(copied_corpus)


def test_pinned_xml_syntax_catalog_counts(spectest_cases):
    schemas = [item for item in spectest_cases if item.kind in SCHEMA_KINDS]
    instances = [item for item in spectest_cases if item.kind in INSTANCE_KINDS]
    assert len(schemas) == SPECTEST_SCHEMAS
    assert len(instances) == SPECTEST_INSTANCES
    assert sum(item.kind == "correct-schema" for item in schemas) == SPECTEST_CORRECT
    assert sum(item.kind == "incorrect-schema" for item in schemas) == SPECTEST_INCORRECT
    assert sum(item.kind == "valid-instance" for item in instances) == SPECTEST_VALID
    assert sum(item.kind == "invalid-instance" for item in instances) == SPECTEST_INVALID
    assert sum(item.profile == PROFILE_XSD_DATATYPE for item in schemas) == (SPECTEST_XSD_SCHEMAS)
    assert sum(item.profile == PROFILE_XSD_DATATYPE for item in instances) == (
        SPECTEST_XSD_INSTANCES
    )


def test_pinned_xml_syntax_catalog_ids_and_files_are_stable(spectest_cases):
    assert len({item.id for item in spectest_cases}) == len(spectest_cases)
    for item in spectest_cases:
        assert item.schema.is_file(), item
        assert item.schema.name in {"c.rng", "i.rng"}
        assert item.schema.read_bytes()
        if item.kind in INSTANCE_KINDS:
            assert item.instance is not None and item.instance.is_file()
        else:
            assert item.instance is None
        assert all(ord(char) < 128 for char in str(item.schema)), item
        if item.instance is not None:
            assert all(ord(char) < 128 for char in str(item.instance)), item


def test_pinned_xml_syntax_catalog_materializes_resources(spectest_cases, copied_corpus):
    root = copied_corpus / CASE_DIRECTORY / "spectest"
    files = [path for path in root.rglob("*") if path.is_file()]
    directories = [path for path in root.rglob("*") if path.is_dir()]
    assert len(files) == SPECTEST_SCHEMAS + SPECTEST_INSTANCES + SPECTEST_RESOURCES
    assert len(directories) == SPECTEST_SCHEMAS + SPECTEST_RESOURCE_DIRS
    assert (root / "0100" / "sub" / "x").is_file()
    assert (root / "0121" / "level1.rng").is_file()
    assert (root / "0121" / "level2.rng").is_file()
    assert "<include" in (root / "0121" / "level1.rng").read_text(encoding="utf-8")


def test_pinned_corpus_materializes_well_formed_xml(spectest_cases, xsdtest_cases):
    for item in spectest_cases + xsdtest_cases:
        assert item.schema.stat().st_size > 0, item
        minidom.parse(str(item.schema))
        if item.instance is not None:
            assert item.instance.stat().st_size > 0, item
            minidom.parse(str(item.instance))


def test_pinned_xml_syntax_catalog_preserves_the_source_fragments(spectest_cases):
    by_id = {item.id: item for item in spectest_cases}
    first = by_id["spectest-0001"].schema.read_text(encoding="utf-8")
    assert "<thisIsJunk/>" in first
    thai = by_id["spectest-0069"].schema.read_text(encoding="utf-8")
    assert "ดี" in thai

    # QName-valued attributes only work when their original prefix survives.
    qname = by_id["spectest-0378"].schema.read_text(encoding="utf-8")
    assert 'xmlns:e="http://www.example.com"' in qname
    assert 'name="e:foo"' in qname
    valid = by_id["spectest-0378/valid-1"].instance
    assert valid is not None
    valid_text = valid.read_text(encoding="utf-8")
    assert "<e:foo" in valid_text
    assert 'xmlns:e="http://www.example.com"' in valid_text


def test_pinned_xsd_datatype_catalog_counts(xsdtest_cases):
    schemas = [item for item in xsdtest_cases if item.kind == "correct-schema"]
    instances = [item for item in xsdtest_cases if item.kind in INSTANCE_KINDS]
    assert len(schemas) == XSDTEST_SCHEMAS
    assert len(instances) == XSDTEST_INSTANCES
    assert {item.profile for item in xsdtest_cases} == {PROFILE_XSD_DATATYPE}
    names = {item.id.removeprefix("xsdtest-").rsplit("-", 1)[0] for item in schemas}
    assert names == XSDTEST_DATATYPE_NAMES
    assert len(names) == XSDTEST_DATATYPES
    assert names.isdisjoint(XSD_1_1_DATATYPES)
    assert {"dateTime", "integer", "QName", "NOTATION", "base64Binary"} <= names
    assert len({item.id for item in xsdtest_cases}) == len(xsdtest_cases)
    for item in xsdtest_cases:
        assert item.schema.is_file(), item
        if item.kind in INSTANCE_KINDS:
            assert item.instance is not None and item.instance.is_file()
        else:
            assert item.instance is None


def test_pinned_xsd_catalog_skips_only_the_documented_xsd11_types(copied_corpus):
    text = (copied_corpus / XSDTEST_FILE).read_text(encoding="utf-8")
    for name in ("untypedAtomic", "anyAtomicType"):
        assert f'name="{name}"' in text
    assert {"untypedAtomic", "anyAtomicType"} == XSD_1_1_DATATYPES


def test_pinned_corpus_provenance_and_license_are_auditable():
    if not CORPUS_AVAILABLE:
        pytest.skip("the pinned corpus is checkout-only")
    provenance = (CORPUS / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "263a35a5e3ebbfccf8eb9752077e59cde9acc9a1" in provenance
    assert "mod/rng-validate/test/spectest.xml" in provenance
    assert "mod/xsd-datatype/test/xsdtest.xml" in provenance
    assert "BSD-3-Clause" in provenance
    sums = (CORPUS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected = {}
    for line in sums:
        digest, name = line.split("  ", 1)
        expected[name] = digest
    assert set(expected) == {"LICENSE", SPECTEST_FILE, XSDTEST_FILE}
    for name, digest in expected.items():
        assert hashlib.sha256((CORPUS / name).read_bytes()).hexdigest() == digest, name
    license_text = (CORPUS / "LICENSE").read_text(encoding="utf-8")
    assert "Thai Open Source Software Center Ltd" in license_text
    assert "Redistribution and use in source and binary forms" in license_text


def test_corpus_cache_and_packaging_are_excluded_from_product_inputs():
    if not (CORPUS / ".gitignore").is_file():
        pytest.skip("the pinned corpus is checkout-only")
    ignore = (CORPUS / ".gitignore").read_text(encoding="utf-8")
    assert ".cases/" in ignore
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    sdist_exclude = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    ruff_exclude = pyproject["tool"]["ruff"]["extend-exclude"]
    assert "tests/relaxng/corpus/**" in sdist_exclude
    assert "tests/relaxng/corpus" in ruff_exclude
