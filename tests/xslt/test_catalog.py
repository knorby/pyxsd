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
    assert [source.path for source in case.environment.sources] == [
        PurePosixPath("shared/source.xml"),
        PurePosixPath("mini/local.xml"),
    ]
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

    function_case = catalog.cases["mini/function-001"]
    assert function_case.invocation.initial_function is not None
    assert function_case.invocation.initial_function.name == "f"
    assert function_case.invocation.initial_function.params[0].name is None


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


def test_catalog_document_and_result_element_helpers() -> None:
    catalog = load_catalog(FIXTURES / "mini-catalog.xml")
    case = catalog.cases["mini/package-001"]

    assert catalog.document(case.source) == FIXTURES / "mini/mini-test-set.xml"
    assert catalog.document(case.environment.sources[0].path) == FIXTURES / "shared/source.xml"
    assert catalog.result_element(case).tag.endswith("result")


def test_corpus_commit_reads_the_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = corpus_commit()
    assert commit == "" or re.fullmatch(r"[0-9a-f]{40}", commit)

    monkeypatch.setattr(catalog_module, "REPO_ROOT", tmp_path)
    assert corpus_commit() == ""


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
