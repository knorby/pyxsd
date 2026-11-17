"""Serialized and secondary results reach the assertion layer.

``<output serialize="yes">`` selects the delivery that retains the serialized
form, and every executed case receives an absolute base output URI so a
relative ``xsl:result-document`` href resolves instead of raising ``XTDE1480``.
These tests drive real compilations and transforms; the executor side is never
hand-built.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from tests.xslt import suite
from tests.xslt.model import Environment, InitialTemplate, Invocation, Output, Stylesheet
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.outcomes import Outcome
from tests.xslt.support import stylesheet

PROFILE = suite.SUITE_PROFILES["xslt30"]


@pytest.fixture(autouse=True)
def _empty_cache():
    """Keep the bounded artifact cache from leaking between tests."""
    suite._COMPILED.clear()
    yield
    suite._COMPILED.clear()


def _sheet(root: Path, name: str, body: str) -> Path:
    """Write one stylesheet with no in-scope prefix to leak into its output."""
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stylesheet(body, declare_xs=False))
    return target


def _case(
    root: Path,
    sheet: str,
    *,
    output: Output | None = None,
    environment_output: Output | None = None,
    result_xml: str = "",
) -> CatalogCase:
    """One synthetic case whose only declared stylesheet is its principal."""
    return CatalogCase(
        name="serialization/example",
        local_name="example",
        test_set="serialization",
        description="",
        source=PurePosixPath("serialization/example.xml"),
        environment=Environment(output=environment_output),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath(sheet)),),
            initial_template=InitialTemplate(name="main"),
            output=output,
        ),
        result_xml=result_xml,
        document_root=root,
    )


def test_serialize_wins_over_tree_for_delivery(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:output method="text" build-tree="no" item-separator="~"/>'
        '<xsl:template name="main"><xsl:sequence select="11 to 20"/></xsl:template>',
    )
    case = _case(tmp_path, "main.xsl", output=Output(serialize=True, tree=False))

    snapshot = suite.execute_case(case, PROFILE)

    assert suite._delivery(case) == "stylesheet"
    assert snapshot.serialized == "11~12~13~14~15~16~17~18~19~20"
    assert snapshot.raw == tuple(range(11, 21))


def test_raw_delivery_still_resolves_relative_result_documents(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:template name="main">'
        '<xsl:result-document href="out.xml"><out/></xsl:result-document>'
        "</xsl:template>",
    )
    case = _case(
        tmp_path,
        "main.xsl",
        output=Output(tree=False, serialize=False),
    )

    snapshot = suite.execute_case(case, PROFILE)

    assert suite._delivery(case) == "raw"
    assert snapshot.serialized is None
    assert snapshot.raw == ()
    assert snapshot.secondary is not None
    assert len(snapshot.secondary) == 1
    (uri, entry) = next(iter(snapshot.secondary.items()))
    assert uri.startswith("file:")
    assert uri.endswith("/out.xml")
    assert entry.xml == "<out />"


def test_a_declared_output_file_is_the_base_output_uri(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:template name="main">'
        "<principal/>"
        '<xsl:result-document href="second/out.xml"><out/></xsl:result-document>'
        "</xsl:template>",
    )
    case = _case(
        tmp_path,
        "main.xsl",
        output=Output(
            file="results/principal.xml",
            path=PurePosixPath("results/principal.xml"),
            tree=True,
        ),
    )

    snapshot = suite.execute_case(case, PROFILE)

    assert suite._delivery(case) == "document"
    assert suite._base_output_uri(case) == (tmp_path / "results/principal.xml").as_uri()
    assert snapshot.xml == "<principal />"
    assert snapshot.serialized is None
    assert snapshot.secondary is not None
    assert list(snapshot.secondary) == [(tmp_path / "results" / "second" / "out.xml").as_uri()]
    assert snapshot.secondary[(tmp_path / "results/second/out.xml").as_uri()].xml == "<out />"


def test_a_relative_result_document_uses_a_synthetic_base(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:template name="main">'
        '<xsl:result-document href="out.xml"><out>hi</out></xsl:result-document>'
        "</xsl:template>",
    )
    case = _case(tmp_path, "main.xsl")

    snapshot = suite.execute_case(case, PROFILE)

    expected = tmp_path / "__pyxsd_output__" / case.name / "out.xml"
    assert snapshot.secondary is not None
    assert list(snapshot.secondary) == [expected.as_uri()]
    assert snapshot.secondary[expected.as_uri()].xml == "<out>hi</out>"


def test_assert_result_document_passes_through_the_base_output_uri(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:template name="main">'
        "<principal/>"
        '<xsl:result-document href="second/out.xml"><out/></xsl:result-document>'
        "</xsl:template>",
    )
    case = _case(
        tmp_path,
        "main.xsl",
        output=Output(
            file="results/principal.xml",
            path=PurePosixPath("results/principal.xml"),
        ),
        result_xml=(
            "<result>"
            '<assert-result-document uri="second/out.xml">'
            "<assert-xml><![CDATA[<out/>]]></assert-xml>"
            "</assert-result-document>"
            "</result>"
        ),
    )

    result = suite.run_case(case, PROFILE)

    assert result.outcome is Outcome.PASS
    assert result.coverage == frozenset({"secondary", "xml"})


def test_a_serialized_secondary_matches_its_pattern(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:template name="main">'
        '<xsl:result-document href="out1.xml" method="xml" omit-xml-declaration="yes">'
        "<out>hello</out>"
        "</xsl:result-document>"
        "</xsl:template>",
    )
    case = _case(
        tmp_path,
        "main.xsl",
        output=Output(serialize=True, tree=False),
        result_xml=(
            "<result>"
            '<assert-result-document uri="out1.xml">'
            "<serialization-matches>^&lt;out&gt;hello&lt;/out&gt;$</serialization-matches>"
            "</assert-result-document>"
            "</result>"
        ),
    )

    result = suite.run_case(case, PROFILE)

    assert result.outcome is Outcome.PASS
    assert {"secondary", "serialization"} <= result.coverage


def test_an_error_result_with_a_secondary_assertion_is_a_failure(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:template name="main"><xsl:sequence select="1 div 0"/></xsl:template>',
    )
    case = _case(
        tmp_path,
        "main.xsl",
        result_xml=(
            "<result>"
            '<assert-result-document uri="out.xml">'
            "<assert-xml><![CDATA[<out/>]]></assert-xml>"
            "</assert-result-document>"
            "</result>"
        ),
    )

    result = suite.run_case(case, PROFILE)

    assert result.outcome is Outcome.FAIL
    assert "FOAR0001" in result.message


def test_a_serialization_method_annotation_does_not_gate_the_text(tmp_path: Path) -> None:
    _sheet(
        tmp_path,
        "main.xsl",
        '<xsl:output method="text"/><xsl:template name="main">hello</xsl:template>',
    )

    matching = _case(
        tmp_path,
        "main.xsl",
        result_xml='<result><assert-serialization method="html">hello</assert-serialization></result>',
    )
    differing = _case(
        tmp_path,
        "main.xsl",
        result_xml='<result><assert-serialization method="html">other</assert-serialization></result>',
    )

    passed = suite.run_case(matching, PROFILE)
    failed = suite.run_case(differing, PROFILE)

    assert passed.outcome is Outcome.PASS
    assert passed.coverage == frozenset({"serialization"})
    assert failed.outcome is Outcome.FAIL
    assert "serialization differs" in failed.message
