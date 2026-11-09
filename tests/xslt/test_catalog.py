"""Catalog parsing against small synthetic corpora and the pinned W3C corpus.

The parser is test infrastructure: it reads the metadata that selects and
describes cases so a separate harness can execute them.  It never imports the
XSLT implementation under test.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from tests.xslt import catalog as catalog_module
from tests.xslt.catalog import CatalogError, corpus_commit, load_catalog, resolve_reference
from tests.xslt.model import Invocation

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = Path(__file__).parent / "corpus"
REPO_ROOT = Path(__file__).parents[2]


def test_catalog_environment_and_package_roles() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/package-001"]
    assert case.spec == "XSLT30"
    assert case.stylesheet_role == "principal"
    assert case.environment.sources[0].role == "source"
    assert case.packages[0].role == "package"


def test_case_and_environment_keep_declared_roles_and_paths() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/package-001"]

    assert case.name == "mini/package-001"
    assert case.local_name == "package-001"
    assert case.test_set == "mini"
    assert case.source == PurePosixPath("mini/mini-test-set.xml")
    assert case.environment.name == "shared-env"
    assert case.environment.ref == "shared-env"

    assert case.environment.schemas[0].role == "source-reference"
    assert case.environment.schemas[0].path == PurePosixPath("mini/schema.xsd")
    assert case.environment.schemas[0].xsd_version == "1.1"
    assert case.environment.resources[0].media_type == "text/plain"
    assert case.environment.resources[0].encoding == "utf-8"
    assert case.environment.collections[0].uri == "collection-1"
    assert case.environment.collections[0].sources[0].role == "source"
    assert case.environment.collections[0].sources[0].path == PurePosixPath("mini/collection-a.xml")
    inline = case.environment.sources[1]
    assert inline.role == "source"
    assert inline.file is None
    assert inline.path is None
    assert inline.content == "<doc><inline/></doc>"
    assert case.environment.collections[0].sources[1].content == "<doc/>"
    assert case.environment.context_item is not None
    assert case.environment.context_item.select == "."
    assert [collation.uri for collation in case.environment.collations] == [
        "http://www.w3.org/2005/xpath-functions/collation/codepoint",
        "http://www.w3.org/2013/collation/UCA?lang=en",
    ]
    assert [collation.default for collation in case.environment.collations] == [True, False]
    assert [package.role for package in case.packages] == ["package", "principal"]
    assert [sheet.role for sheet in case.stylesheets] == ["principal"]
    assert case.source_document is True


def test_nested_catalog_includes_resolve_relative_to_their_document() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["nested/nested-001"]

    assert "nested" in catalog.test_sets
    assert case.source == PurePosixPath("mini/nested/nested-test-set.xml")
    assert case.environment.sources[0].path == PurePosixPath("shared/nested-source.xml")
    assert case.stylesheets[0].path == PurePosixPath("mini/nested/nested-001.xsl")
    assert case.specs == ("XSLT30",)


def test_xml_base_is_honoured() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    based = catalog.cases["mini/based-001"]

    assert based.environment.sources[0].path == PurePosixPath("shared/based.xml")


def test_shared_environment_is_merged_with_local_declarations() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/local-override"]

    assert case.environment.ref == "shared-env"
    assert [source.path for source in case.environment.sources if source.path is not None] == [
        PurePosixPath("shared/source.xml"),
        PurePosixPath("mini/local.xml"),
    ]
    assert case.environment.sources[1].content == "<doc><inline/></doc>"
    assert [param.name for param in case.environment.params] == ["env-param", "local-param"]
    assert case.environment.output is not None
    assert case.environment.output.tree is None
    assert case.environment.output.serialize is True


def test_catalog_level_environments_are_visible_to_test_sets() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/global-001"]

    assert case.environment.ref == "global-env"
    assert case.environment.sources[0].path == PurePosixPath("shared/global.xml")


def test_invocation_data_is_preserved() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/package-001"]
    invocation = case.invocation

    assert invocation.packages[0].file == "package-001.xsl"
    assert invocation.packages[0].path == PurePosixPath("mini/package-001.xsl")
    assert invocation.params[0].name == "arg"
    assert invocation.params[0].select == "41"
    assert invocation.params[0].as_type == "xs:integer"
    assert invocation.initial_template is not None
    assert invocation.initial_template.name == "main"
    assert invocation.initial_template.params[0].name == "tunnel-arg"
    assert invocation.initial_template.params[0].tunnel is True
    assert invocation.output is not None
    assert invocation.output.path == PurePosixPath("mini/out")
    assert invocation.output.result_var == "result"
    assert invocation.modules == invocation.packages + invocation.stylesheets

    function_case = catalog.cases["mini/function-001"]
    assert function_case.invocation.initial_function is not None
    assert function_case.invocation.initial_function.name == "f"
    assert function_case.invocation.initial_function.params[0].name is None


def test_initial_mode_and_posture_and_sweep_are_preserved() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    invocation = catalog.cases["mini/mode-001"].invocation

    assert invocation.initial_mode is not None
    assert invocation.initial_mode.name == "start"
    assert invocation.initial_mode.select == "42"
    assert invocation.initial_mode.params[0].name == "mode-arg"
    assert invocation.initial_mode.params[0].tunnel is True
    assert invocation.posture_and_sweep is not None
    assert "posture-and-sweep" in invocation.posture_and_sweep
    assert "xpath" in invocation.posture_and_sweep


def test_stylesheet_role_handles_missing_and_secondary_modules() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")

    package_only = catalog.cases["mini/package-only-001"]
    assert package_only.stylesheets == ()
    assert package_only.stylesheet_role is None
    assert package_only.source_document is True

    secondary = catalog.cases["mini/secondary-001"]
    assert secondary.stylesheets[0].role == "secondary"
    assert secondary.stylesheets[0].path == PurePosixPath("mini/included.xsl")
    assert secondary.stylesheet_role == "secondary"
    assert secondary.source_document is False


def test_dependencies_are_inherited_and_exclusions_are_kept() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/function-001"]

    assert case.specs == ("XSLT30",)
    assert case.spec == "XSLT30"
    assert case.features == frozenset({"higher_order_functions"})
    assert case.feature_exclusions == frozenset({"schema_aware"})


def test_result_assertions_are_retained_as_xml() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/package-001"]

    assert "all-of" in case.result_xml
    assert "assert-count" in case.result_xml


def test_duplicate_case_is_rejected() -> None:
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(FIXTURES / "duplicate-catalog.xml")


def test_missing_catalog_reports_the_corpus_command(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="git submodule update --init tests/xslt/corpus"):
        load_catalog(tmp_path / "absent-catalog.xml")


def test_missing_test_set_document_raises_catalog_error() -> None:
    with pytest.raises(CatalogError, match="absent-test-set"):
        load_catalog(FIXTURES / "missing-ref-catalog.xml")


def test_unresolved_environment_is_a_metadata_error() -> None:
    catalog = load_catalog(FIXTURES / "broken-catalog.xml")
    case = catalog.cases["broken/broken-001"]

    assert case.environment.ref == "absent-env"
    assert case.environment.sources == ()
    assert any("absent-env" in error for error in catalog.metadata_errors)


def test_resolve_reference_normalises_and_rejects_non_local() -> None:
    base = PurePosixPath("tests/group")
    assert resolve_reference(base, "../shared/x.xml") == PurePosixPath("tests/shared/x.xml")
    assert resolve_reference(base, "a/./b.xml#frag") == PurePosixPath("tests/group/a/b.xml")
    assert resolve_reference(base, "a/b.xml?query") == PurePosixPath("tests/group/a/b.xml")
    assert resolve_reference(PurePosixPath("."), "../../x.xml") == PurePosixPath("x.xml")
    assert resolve_reference(base, "https://example.org/x.xml") is None
    assert resolve_reference(base, "/absolute/x.xml") is None
    assert resolve_reference(base, "") is None
    assert resolve_reference(base, "#frag") is None
    assert resolve_reference(base, "?query") is None
    assert resolve_reference(base, None) is None


def test_non_catalog_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "catalog.xml"
    path.write_text(
        '<not-a-catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog"/>',
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="expected <catalog>"):
        load_catalog(path)


def test_cyclic_catalog_include_is_rejected(tmp_path: Path) -> None:
    namespace = 'xmlns="http://www.w3.org/2012/10/xslt-test-catalog"'
    (tmp_path / "a.xml").write_text(
        f'<catalog {namespace}><test-set name="a" file="b.xml"/></catalog>',
        encoding="utf-8",
    )
    (tmp_path / "b.xml").write_text(
        f'<test-set {namespace} name="b"><test-set name="a" file="a.xml"/></test-set>',
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="cyclic"):
        load_catalog(tmp_path / "a.xml")


def test_test_set_without_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "catalog.xml"
    path.write_text(
        '<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">'
        '<test-set name="broken"/></catalog>',
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="no file"):
        load_catalog(path)


def test_incomplete_case_is_kept_with_metadata_errors(tmp_path: Path) -> None:
    path = tmp_path / "test-set.xml"
    path.write_text(
        '<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="incomplete">'
        '<test-case name="incomplete-001">'
        "<description>Missing test and result.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        "</test-case></test-set>",
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    case = catalog.cases["incomplete/incomplete-001"]

    assert case.invocation == Invocation()
    assert case.result_xml == ""
    assert any("no <test>" in error for error in catalog.metadata_errors)
    assert any("no <result>" in error for error in catalog.metadata_errors)


def test_no_namespace_document_and_missing_dependencies(tmp_path: Path) -> None:
    (tmp_path / "catalog.xml").write_text(
        '<catalog><test-set name="plain" file="plain.xml"/></catalog>',
        encoding="utf-8",
    )
    (tmp_path / "plain.xml").write_text(
        '<test-set name="plain"><test-case name="plain-001">'
        "<description>No namespace.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        '<test><stylesheet file="plain-001.xsl"/></test>'
        "<result><assert>true()</assert></result>"
        "</test-case></test-set>",
        encoding="utf-8",
    )
    catalog = load_catalog(tmp_path / "catalog.xml")
    case = catalog.cases["plain/plain-001"]

    assert catalog.metadata_errors == ()
    assert case.spec is None
    assert case.specs == ()
    assert case.features == frozenset()
    assert case.feature_exclusions == frozenset()
    assert case.stylesheet_role == "principal"


def test_invalid_boolean_empty_attributes_and_absent_output(tmp_path: Path) -> None:
    path = tmp_path / "test-set.xml"
    path.write_text(
        '<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="edges">'
        '<test-case name="edges-001">'
        "<description>Unusual attribute values.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        '<test><stylesheet file="edges.xsl"/><param name="" select="   "/>'
        '<output file="#absent" serialize="maybe"/></test>'
        "<result><assert>true()</assert></result>"
        "</test-case></test-set>",
        encoding="utf-8",
    )
    invocation = load_catalog(path).cases["edges/edges-001"].invocation

    assert invocation.params[0].name is None
    assert invocation.params[0].select is None
    assert invocation.output is not None
    assert invocation.output.file == "#absent"
    assert invocation.output.path is None
    assert invocation.output.serialize is None
    assert invocation.output.tree is None
    assert invocation.output.well_formed is None


def test_document_path_outside_root_falls_back_to_name(tmp_path: Path) -> None:
    loader = catalog_module._CatalogLoader(tmp_path / "root")

    assert loader._document_path(tmp_path / "outside.xml") == PurePosixPath("outside.xml")


def test_malformed_catalog_document_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "catalog.xml").write_text(
        '<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">'
        '<test-set name="bad" file="bad.xml"/></catalog>',
        encoding="utf-8",
    )
    (tmp_path / "bad.xml").write_text("<test-set", encoding="utf-8")

    with pytest.raises(CatalogError, match="cannot read catalog document"):
        load_catalog(tmp_path / "catalog.xml")


def test_foreign_namespace_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "catalog.xml"
    path.write_text('<foo xmlns="urn:not-the-catalog"/>', encoding="utf-8")

    with pytest.raises(CatalogError, match="not an XSLT test catalog document"):
        load_catalog(path)


def test_catalog_environment_metadata_errors_are_reported(tmp_path: Path) -> None:
    (tmp_path / "set.xml").write_text(
        '<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="set">'
        '<test-case name="case-001">'
        "<description>Minimal case.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        '<test><stylesheet file="case-001.xsl"/></test>'
        "<result><assert>true()</assert></result>"
        "</test-case></test-set>",
        encoding="utf-8",
    )
    (tmp_path / "catalog.xml").write_text(
        '<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">'
        '<environment><param name="p" select="1"/></environment>'
        '<environment name="dup"><param name="a" select="1"/></environment>'
        '<environment name="dup"><param name="b" select="2"/></environment>'
        '<test-set name="set" file="set.xml"/></catalog>',
        encoding="utf-8",
    )
    catalog = load_catalog(tmp_path / "catalog.xml")
    errors = "\n".join(catalog.metadata_errors)

    assert "environment without a name" in errors
    assert "duplicate environment 'dup'" in errors


def test_unnamed_test_set_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "test-set.xml"
    path.write_text(
        '<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog">'
        '<test-case name="case-001">'
        "<description>Minimal case.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        '<test><stylesheet file="case-001.xsl"/></test>'
        "<result><assert>true()</assert></result>"
        "</test-case></test-set>",
        encoding="utf-8",
    )
    catalog = load_catalog(path)

    assert catalog.cases["/case-001"].local_name == "case-001"
    assert any("test-set without a name" in error for error in catalog.metadata_errors)


def test_test_set_environment_metadata_errors_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "test-set.xml"
    path.write_text(
        '<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="set">'
        '<environment><param name="p" select="1"/></environment>'
        '<environment name="dup"><param name="a" select="1"/></environment>'
        '<environment name="dup"><param name="b" select="2"/></environment>'
        '<test-case name="case-001">'
        "<description>Minimal case.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        '<test><stylesheet file="case-001.xsl"/></test>'
        "<result><assert>true()</assert></result>"
        "</test-case></test-set>",
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    errors = "\n".join(catalog.metadata_errors)

    assert "environment without a name" in errors
    assert "duplicate environment 'dup'" in errors


def test_unusable_test_set_reference_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "catalog.xml"
    path.write_text(
        '<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">'
        '<test-set name="remote" file="https://example.org/x.xml"/></catalog>',
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="unusable file"):
        load_catalog(path)


def test_unnamed_test_case_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "test-set.xml"
    path.write_text(
        '<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="set">'
        "<test-case>"
        "<description>Minimal case without a name.</description>"
        '<created by="Mini Author" on="2026-01-01"/>'
        '<test><stylesheet file="case-001.xsl"/></test>'
        "<result><assert>true()</assert></result>"
        "</test-case></test-set>",
        encoding="utf-8",
    )
    catalog = load_catalog(path)

    assert catalog.cases["set/unnamed-1"].local_name == "unnamed-1"
    assert any("test-case without a name" in error for error in catalog.metadata_errors)


def test_catalog_document_and_result_element_helpers() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/package-001"]

    assert catalog.document(case.source) == FIXTURES / "mini/mini-test-set.xml"
    assert catalog.document(case.environment.sources[0].path) == FIXTURES / "shared/source.xml"
    assert catalog.result_element(case).tag.endswith("result")


def test_corpus_commit_reads_the_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_revision = catalog_module._git_revision
    commit = corpus_commit()
    assert commit == "" or re.fullmatch(r"[0-9a-f]{40}", commit)

    monkeypatch.setattr(catalog_module, "_git_revision", lambda arguments: "a" * 40)
    assert corpus_commit() == "a" * 40

    def gitlink_only(arguments: list[str]) -> str:
        return "" if arguments[0] == "-C" else "b" * 40

    monkeypatch.setattr(catalog_module, "_git_revision", gitlink_only)
    assert corpus_commit() == "b" * 40

    monkeypatch.setattr(catalog_module, "_git_revision", real_revision)
    monkeypatch.setattr(catalog_module, "REPO_ROOT", tmp_path)
    assert corpus_commit() == ""


def test_git_revision_rejects_non_sha_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class Completed:
        stdout = "not-a-sha\n"

    monkeypatch.setattr(catalog_module.subprocess, "run", lambda *args, **kwargs: Completed())

    assert catalog_module._git_revision(["rev-parse", "HEAD"]) == ""


def test_corpus_is_excluded_from_sdist() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "tests/xslt/corpus/**" in excludes


@pytest.mark.skipif(not (CORPUS / "catalog.xml").is_file(), reason="XSLT corpus not checked out")
def test_real_catalog_parses_without_metadata_errors() -> None:
    catalog = load_catalog(CORPUS / "catalog.xml")

    assert catalog.cases
    assert len(catalog.cases) > 10000
    assert catalog.metadata_errors == ()
    assert re.fullmatch(r"[0-9a-f]{40}", catalog.corpus_revision)

    package_case = catalog.cases["package/package-001"]
    assert package_case.spec == "XSLT30+"
    assert package_case.invocation.packages[0].role == "principal"
    assert package_case.invocation.initial_template is not None
    assert package_case.invocation.initial_template.name == "main"

    strip_case = catalog.cases["strip-space/strip-space-001"]
    assert strip_case.environment.ref == "strip-space-01"
    assert strip_case.environment.sources[0].role == "."
    assert strip_case.environment.sources[0].content is not None
    assert strip_case.environment.schemas == ()
    assert strip_case.stylesheets[0].role == "stylesheet"
    assert strip_case.stylesheets[0].path == PurePosixPath(
        "tests/decl/strip-space/strip-space-001.xsl"
    )
    assert strip_case.stylesheet_role == "principal"
