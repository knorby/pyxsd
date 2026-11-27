"""Corpus cases whose invocation selects one of several principal stylesheets.

A test set may declare several non-secondary stylesheets and have the test name
the one to execute.  The executor compiles that selection, falls back to a lone
principal when the test names none, and refuses every selection the catalog
leaves ambiguous: several principals with no choice, a module promoted by the
test, several named files, or a selection without a file.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from tests.xslt import suite
from tests.xslt.model import (
    Environment,
    InitialMode,
    InitialTemplate,
    Invocation,
    Package,
    Param,
    Stylesheet,
)
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.support import stylesheet

PROFILE = suite.SUITE_PROFILES["xslt30"]

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

PRINCIPAL_PACKAGE = "urn:example:principal"


@pytest.fixture(autouse=True)
def _empty_cache():
    """Keep the bounded artifact cache from leaking between tests."""
    suite._COMPILED.clear()
    yield
    suite._COMPILED.clear()


def _body(marker: str) -> str:
    """One initial template printing *marker*."""
    return f'<xsl:template name="main"><out>{marker}</out></xsl:template>'


def _write(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.write_bytes(stylesheet(body, declare_xs=False))
    return target


def _declaration(name: str) -> Stylesheet:
    return Stylesheet(role="stylesheet", path=PurePosixPath(name))


def _case(
    root: Path,
    *,
    environment_stylesheets: tuple[Stylesheet, ...] = (),
    stylesheets: tuple[Stylesheet, ...] = (),
    params: tuple[Param, ...] = (),
) -> CatalogCase:
    return CatalogCase(
        name="selection/example",
        local_name="example",
        test_set="selection",
        description="",
        source=PurePosixPath("selection/example.xml"),
        environment=Environment(stylesheets=environment_stylesheets),
        invocation=Invocation(
            stylesheets=stylesheets,
            params=params,
            initial_template=InitialTemplate(name="main"),
        ),
        document_root=root,
    )


def test_the_named_stylesheet_is_the_compiled_principal(tmp_path: Path) -> None:
    _write(tmp_path, "env.xsl", _body("env"))
    named = _write(tmp_path, "named.xsl", _body("named"))
    case = _case(
        tmp_path,
        environment_stylesheets=(_declaration("env.xsl"),),
        stylesheets=(_declaration("named.xsl"),),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>named</out>"
    assert suite._compilation_units(case).principal == named


@pytest.mark.parametrize(
    ("environment_stylesheets", "reason"),
    [
        ((_declaration("first.xsl"), _declaration("second.xsl")), "2 principal stylesheets"),
        ((), "0 principal stylesheets"),
    ],
    ids=["several", "none"],
)
def test_no_selection_with_several_or_no_principals_is_unsupported(
    tmp_path: Path,
    environment_stylesheets: tuple[Stylesheet, ...],
    reason: str,
) -> None:
    _write(tmp_path, "first.xsl", _body("first"))
    _write(tmp_path, "second.xsl", _body("second"))
    case = _case(tmp_path, environment_stylesheets=environment_stylesheets)

    with pytest.raises(suite.UnsupportedCase, match=reason):
        suite.execute_case(case, PROFILE)


def test_selecting_an_environment_secondary_module_is_unsupported(tmp_path: Path) -> None:
    _write(tmp_path, "main.xsl", _body("main"))
    _write(tmp_path, "mod.xsl", _body("mod"))
    case = _case(
        tmp_path,
        environment_stylesheets=(
            Stylesheet(role="principal", path=PurePosixPath("main.xsl")),
            Stylesheet(role="secondary", path=PurePosixPath("mod.xsl")),
        ),
        stylesheets=(_declaration("mod.xsl"),),
    )

    with pytest.raises(suite.UnsupportedCase, match="selected stylesheet is a secondary module"):
        suite.execute_case(case, PROFILE)


def test_two_distinct_named_stylesheets_are_unsupported(tmp_path: Path) -> None:
    _write(tmp_path, "a.xsl", _body("a"))
    _write(tmp_path, "b.xsl", _body("b"))
    case = _case(tmp_path, stylesheets=(_declaration("a.xsl"), _declaration("b.xsl")))

    with pytest.raises(suite.UnsupportedCase, match="2 selected stylesheets"):
        suite.execute_case(case, PROFILE)


def test_one_file_declared_more_than_once_is_one_principal(tmp_path: Path) -> None:
    """The environment and the test may both name the same stylesheet."""
    main = _write(tmp_path, "main.xsl", _body("once"))
    repeated = _case(
        tmp_path,
        environment_stylesheets=(_declaration("main.xsl"),),
        stylesheets=(_declaration("main.xsl"),),
    )

    assert suite._compilation_units(repeated) == suite._Units(principal=main)
    assert suite.execute_case(repeated, PROFILE).xml == "<out>once</out>"

    # An environment declaration that also names the file non-secondary
    # promotes a module back; the test's selection is then ordinary.
    promoted = _case(
        tmp_path,
        environment_stylesheets=(
            Stylesheet(role="secondary", path=PurePosixPath("main.xsl")),
            _declaration("main.xsl"),
        ),
        stylesheets=(_declaration("main.xsl"),),
    )

    assert suite.execute_case(promoted, PROFILE).xml == "<out>once</out>"


@pytest.mark.parametrize("named", [True, False], ids=["named", "lone"])
def test_a_selection_without_a_file_is_unsupported(tmp_path: Path, named: bool) -> None:
    declaration = Stylesheet(role="stylesheet", uri="urn:example:x")
    case = (
        _case(tmp_path, stylesheets=(declaration,))
        if named
        else _case(tmp_path, environment_stylesheets=(declaration,))
    )

    with pytest.raises(suite.UnsupportedCase, match="principal stylesheet without a file"):
        suite.execute_case(case, PROFILE)


def test_a_lone_principal_is_the_fallback_under_packages_too(tmp_path: Path) -> None:
    _write(tmp_path, "lone.xsl", _body("lone"))
    lone = _case(tmp_path, environment_stylesheets=(_declaration("lone.xsl"),))

    assert suite.execute_case(lone, PROFILE).xml == "<out>lone</out>"

    principal = tmp_path / "package.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" name="{PRINCIPAL_PACKAGE}" version="3.0">
  <xsl:template name="main" visibility="public"><out>pkg</out></xsl:template>
</xsl:package>""",
        encoding="utf-8",
    )
    _write(tmp_path, "named.xsl", _body("named"))
    packaged = _case(tmp_path, stylesheets=(_declaration("named.xsl"),))
    packaged = replace(
        packaged,
        invocation=replace(
            packaged.invocation,
            packages=(Package(role="principal", path=PurePosixPath("package.xsl")),),
        ),
    )

    assert suite._compilation_units(packaged).principal == principal
    assert suite.execute_case(packaged, PROFILE).xml == "<out>pkg</out>"


def test_two_selected_principals_are_two_cached_artifacts(tmp_path: Path) -> None:
    _write(tmp_path, "env.xsl", _body("env"))
    _write(tmp_path, "a.xsl", _body("a"))
    _write(tmp_path, "b.xsl", _body("b"))
    first = _case(
        tmp_path,
        environment_stylesheets=(_declaration("env.xsl"),),
        stylesheets=(_declaration("a.xsl"),),
    )
    second = _case(
        tmp_path,
        environment_stylesheets=(_declaration("env.xsl"),),
        stylesheets=(_declaration("b.xsl"),),
    )

    assert suite.execute_case(first, PROFILE).xml == "<out>a</out>"
    assert suite.execute_case(second, PROFILE).xml == "<out>b</out>"
    assert len(suite._COMPILED) == 2

    cached = {key: id(artifact) for key, artifact in suite._COMPILED.items()}
    assert suite.execute_case(first, PROFILE).xml == "<out>a</out>"
    assert {key: id(artifact) for key, artifact in suite._COMPILED.items()} == cached


def test_static_parameters_bypass_the_cache_for_a_selected_principal(tmp_path: Path) -> None:
    _write(tmp_path, "env.xsl", _body("env"))
    _write(
        tmp_path,
        "named.xsl",
        '<xsl:param name="which" select="\'env\'" static="yes"/>'
        '<xsl:template name="main"><out><xsl:value-of select="$which"/></out></xsl:template>',
    )
    parameterized = _case(
        tmp_path,
        environment_stylesheets=(_declaration("env.xsl"),),
        stylesheets=(_declaration("named.xsl"),),
        params=(Param(name="which", select="'named'", static=True),),
    )
    plain = _case(
        tmp_path,
        environment_stylesheets=(_declaration("env.xsl"),),
        stylesheets=(_declaration("named.xsl"),),
    )

    assert suite.execute_case(parameterized, PROFILE).xml == "<out>named</out>"
    assert suite._COMPILED == {}
    assert suite.execute_case(plain, PROFILE).xml == "<out>env</out>"
    assert len(suite._COMPILED) == 1


def test_a_selection_without_a_corpus_root_is_unsupported(tmp_path: Path) -> None:
    _write(tmp_path, "main.xsl", _body("main"))
    case = replace(
        _case(tmp_path, stylesheets=(_declaration("main.xsl"),)),
        document_root=None,
    )

    with pytest.raises(suite.UnsupportedCase, match="case without a corpus root"):
        suite.execute_case(case, PROFILE)


def test_a_coded_invocation_error_is_reported_as_that_error_code(tmp_path: Path) -> None:
    """A host invocation error that carries an XSLT code is a case outcome.

    An initial mode without a match selection is XTDE0044 (XSLT 3.0 section
    2.3.2).  The processor rejects it before compiling a transformation, so the
    failure reaches a host caller as ``XsltInvocationError`` with the code; the
    executor maps it exactly like an engine ``XsltError`` instead of recording
    an uncoded harness failure.  The synthetic case is the corpus shape of
    ``package/package-914b``.
    """
    _write(tmp_path, "mode.xsl", '<xsl:template match="/"><out/></xsl:template>')
    base = _case(tmp_path, environment_stylesheets=(_declaration("mode.xsl"),))
    case = replace(
        base,
        invocation=Invocation(initial_mode=InitialMode(name="#default")),
        result_xml='<result><error code="XTDE0044"/></result>',
    )

    result = suite.run_case(case, PROFILE)

    assert result.outcome.value == "pass"
    assert result.expected_error_code == "XTDE0044"
