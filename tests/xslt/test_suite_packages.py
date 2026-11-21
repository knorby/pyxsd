"""Corpus cases that declare packages compile through an explicit resolver.

A package-based case names one principal compilation unit and the secondary
packages it reaches with ``xsl:use-package``.  The executor registers every
secondary package on a ``PackageResolver`` so the principal's reference
resolves, and the compiled-artifact cache covers the whole package closure so
two cases with the same principal and different libraries never share one
artifact.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from tests.xslt import suite
from tests.xslt.model import Environment, Invocation, Package, Param, Stylesheet
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.runner import CaseExecutionError
from tests.xslt.support import stylesheet

PROFILE = suite.SUITE_PROFILES["xslt30"]

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

#: The package names the synthetic cases use.  The resolver keys on the name a
#: package declares, never on the file it lives in.
LIBRARY = "urn:example:lib"
PRINCIPAL = "urn:example:principal"


@pytest.fixture(autouse=True)
def _empty_cache():
    """Keep the bounded artifact cache from leaking between tests."""
    suite._COMPILED.clear()
    yield
    suite._COMPILED.clear()


def _write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.write_text(text, encoding="utf-8")
    return target


def _library(value: str = "'hello'") -> str:
    """A library package exposing one public function."""
    return f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="{LIBRARY}"
    name="{LIBRARY}" version="3.0">
  <xsl:function name="lib:greet" visibility="public">
    <xsl:sequence select="{value}"/>
  </xsl:function>
</xsl:package>"""


def _principal(*, name: str = PRINCIPAL, declarations: str = "") -> str:
    """A principal package that calls the library's public function."""
    return f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="{LIBRARY}"
    exclude-result-prefixes="lib" name="{name}" version="3.0">
  {declarations}
  <xsl:use-package name="{LIBRARY}"/>
  <xsl:template name="xsl:initial-template" visibility="public">
    <out><xsl:value-of select="lib:greet()"/></out>
  </xsl:template>
</xsl:package>"""


def _stylesheet_principal() -> str:
    """An ordinary stylesheet that reaches the same library package."""
    return f"""<xsl:transform xmlns:xsl="{XSLT_NS}" xmlns:lib="{LIBRARY}"
    exclude-result-prefixes="lib" version="3.0">
  <xsl:use-package name="{LIBRARY}"/>
  <xsl:template name="xsl:initial-template">
    <out><xsl:value-of select="lib:greet()"/></out>
  </xsl:template>
</xsl:transform>"""


def _case(
    root: Path,
    *,
    packages: tuple[Package, ...] = (),
    stylesheets: tuple[Stylesheet, ...] = (),
    environment_packages: tuple[Package, ...] = (),
    params: tuple[Param, ...] = (),
) -> CatalogCase:
    return CatalogCase(
        name="packages/example",
        local_name="example",
        test_set="packages",
        description="",
        source=PurePosixPath("packages/example.xml"),
        environment=Environment(packages=environment_packages),
        invocation=Invocation(packages=packages, stylesheets=stylesheets, params=params),
        document_root=root,
    )


def _unit_case(
    root: Path,
    *,
    principal: str = "principal.xsl",
    libraries: tuple[str, ...] = ("library.xsl",),
    params: tuple[Param, ...] = (),
) -> CatalogCase:
    """A package case whose principal is in the test and libraries in the environment."""
    return _case(
        root,
        packages=(Package(role="principal", path=PurePosixPath(principal)),),
        environment_packages=tuple(
            Package(role="secondary", path=PurePosixPath(name)) for name in libraries
        ),
        params=params,
    )


def test_a_package_case_compiles_with_its_library_registered(tmp_path: Path) -> None:
    _write(tmp_path, "principal.xsl", _principal())
    _write(tmp_path, "library.xsl", _library("'hello'"))

    snapshot = suite.execute_case(_unit_case(tmp_path), PROFILE)

    assert snapshot.xml == "<out>hello</out>"


def test_package_roles_choose_the_compiled_unit(tmp_path: Path) -> None:
    principal = _write(tmp_path, "principal.xsl", _principal())
    library = _write(tmp_path, "library.xsl", _library())

    units = suite._compilation_units(_unit_case(tmp_path))

    assert units.principal == principal
    assert units.libraries == (library,)
    assert units.principal.is_absolute()
    assert units.libraries[0].is_absolute()


def test_two_principal_packages_are_unsupported(tmp_path: Path) -> None:
    _write(tmp_path, "first.xsl", _principal(name="urn:example:first"))
    _write(tmp_path, "second.xsl", _principal(name="urn:example:second"))
    case = _case(
        tmp_path,
        packages=(
            Package(role="principal", path=PurePosixPath("first.xsl")),
            Package(role="principal", path=PurePosixPath("second.xsl")),
        ),
    )

    with pytest.raises(suite.UnsupportedCase, match="2 principal packages"):
        suite._compilation_units(case)


def test_a_principal_package_without_a_file_is_unsupported(tmp_path: Path) -> None:
    case = _case(tmp_path, packages=(Package(role="principal", uri=PRINCIPAL),))

    with pytest.raises(suite.UnsupportedCase, match="principal package without a file"):
        suite._compilation_units(case)


def test_a_secondary_package_without_a_file_is_unsupported(tmp_path: Path) -> None:
    _write(tmp_path, "principal.xsl", _principal())
    case = _case(
        tmp_path,
        packages=(Package(role="principal", path=PurePosixPath("principal.xsl")),),
        environment_packages=(Package(role="secondary", uri=LIBRARY),),
    )

    with pytest.raises(suite.UnsupportedCase, match="secondary package without a file"):
        suite._compilation_units(case)


def test_a_package_case_without_a_corpus_root_is_unsupported(tmp_path: Path) -> None:
    case = replace(_unit_case(tmp_path), document_root=None)

    with pytest.raises(suite.UnsupportedCase, match="case without a corpus root"):
        suite._compilation_units(case)


def test_a_library_change_invalidates_the_compiled_artifact(tmp_path: Path) -> None:
    _write(tmp_path, "principal.xsl", _principal())
    library = _write(tmp_path, "library.xsl", _library("'first'"))
    case = _unit_case(tmp_path)

    first = suite._compile_stylesheet(PROFILE, suite._compilation_units(case), {})
    assert first.transform().principal_tree.string_value == "first"

    library.write_text(_library("'second'"), encoding="utf-8")
    second = suite._compile_stylesheet(PROFILE, suite._compilation_units(case), {})

    assert second is not first
    assert second.transform().principal_tree.string_value == "second"
    assert len(suite._COMPILED) == 2


def test_an_unchanged_package_case_reuses_one_artifact(tmp_path: Path) -> None:
    _write(tmp_path, "principal.xsl", _principal())
    _write(tmp_path, "library.xsl", _library())
    case = _unit_case(tmp_path)

    first = suite._compile_stylesheet(PROFILE, suite._compilation_units(case), {})
    second = suite._compile_stylesheet(PROFILE, suite._compilation_units(case), {})

    assert second is first
    assert len(suite._COMPILED) == 1


def test_a_case_without_packages_keeps_the_plain_processor(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "sheet.xsl",
        stylesheet('<xsl:template name="xsl:initial-template"><out/></xsl:template>').decode(),
    )
    case = _case(
        tmp_path,
        stylesheets=(Stylesheet(role="principal", path=PurePosixPath("sheet.xsl")),),
    )

    units = suite._compilation_units(case)
    processor = suite._processor_for(PROFILE, units)

    assert units.libraries == ()
    assert processor is PROFILE.processor
    assert processor.resolver is None


def test_a_stylesheet_principal_carries_the_secondary_packages(tmp_path: Path) -> None:
    """An ordinary principal stylesheet may use packages, as 43 corpus cases do."""
    principal = _write(tmp_path, "sheet.xsl", _stylesheet_principal())
    library = _write(tmp_path, "library.xsl", _library("'hello'"))
    case = _case(
        tmp_path,
        stylesheets=(Stylesheet(role="principal", path=PurePosixPath("sheet.xsl")),),
        environment_packages=(Package(role="secondary", path=PurePosixPath("library.xsl")),),
    )

    units = suite._compilation_units(case)

    assert units == suite._Units(principal=principal, libraries=(library,))
    assert suite.execute_case(case, PROFILE).xml == "<out>hello</out>"


def test_repeating_one_package_declaration_is_one_unit(tmp_path: Path) -> None:
    """The environment and the test may both name the same principal file."""
    principal = _write(tmp_path, "principal.xsl", _principal())
    library = _write(tmp_path, "library.xsl", _library())
    declaration = Package(role="principal", path=PurePosixPath("principal.xsl"))
    shared = Package(role="secondary", path=PurePosixPath("library.xsl"))
    case = _case(
        tmp_path,
        packages=(declaration,),
        environment_packages=(declaration, shared, shared),
    )

    assert suite._compilation_units(case) == suite._Units(
        principal=principal,
        libraries=(library,),
    )


def test_packages_without_a_principal_are_unsupported(tmp_path: Path) -> None:
    _write(tmp_path, "library.xsl", _library())
    case = _case(
        tmp_path,
        environment_packages=(Package(role="secondary", path=PurePosixPath("library.xsl")),),
    )

    with pytest.raises(suite.UnsupportedCase, match="packages without a principal"):
        suite._compilation_units(case)


def test_a_missing_library_surfaces_the_engine_error(tmp_path: Path) -> None:
    _write(tmp_path, "principal.xsl", _principal())

    with pytest.raises(CaseExecutionError, match="XTSE0165"):
        suite.execute_case(_unit_case(tmp_path), PROFILE)


def test_a_static_parameter_case_still_registers_the_library(tmp_path: Path) -> None:
    declaration = '<xsl:param name="flag" select="\'yes\'" static="yes"/>'
    _write(tmp_path, "principal.xsl", _principal(declarations=declaration))
    _write(tmp_path, "library.xsl", _library("'static'"))
    case = _unit_case(tmp_path, params=(Param(name="flag", select="'no'", static=True),))

    snapshot = suite.execute_case(case, PROFILE)

    assert snapshot.xml == "<out>static</out>"
    assert suite._COMPILED == {}
