"""Application authorization of stylesheet-selected output destinations."""

import io

import pytest
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xslt import OutputPolicy
from pyxsd.xslt.processor import XsltProcessor


def test_output_policy_denies_secondary_before_evaluating_content() -> None:
    class Deny:
        def authorize(self, uri: str, *, principal: bool) -> None:
            assert uri == "https://example.test/secondary.xml"
            assert principal is False
            raise ResourcePolicyError("output denied")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/secondary.xml">'
        '<xsl:sequence select="error()"/>'
        "</xsl:result-document></xsl:template>"
    )
    compiled = XsltProcessor(output_policy=Deny()).compile(io.BytesIO(source))
    with pytest.raises(ResourcePolicyError, match="output denied"):
        compiled.transform()


def test_output_policy_denies_principal_before_stylesheet_execution() -> None:
    class Deny:
        def authorize(self, uri: str, *, principal: bool) -> None:
            assert uri == "https://example.test/principal.xml"
            assert principal is True
            raise ResourcePolicyError("principal denied")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template"><xsl:sequence select="error()"/></xsl:template>'
    )
    compiled = XsltProcessor(output_policy=Deny()).compile(io.BytesIO(source))
    with pytest.raises(ResourcePolicyError, match="principal denied"):
        compiled.transform(output_uri="https://example.test/principal.xml")


def test_output_policy_allows_in_memory_results_without_writes(tmp_path) -> None:
    class Allow:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def authorize(self, uri: str, *, principal: bool) -> None:
            self.calls.append((uri, principal))

    policy = Allow()
    target = tmp_path / "secondary.xml"
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{target.as_uri()}"><secondary/></xsl:result-document>'
        "<primary/></xsl:template>"
    )
    compiled = XsltProcessor(output_policy=policy).compile(io.BytesIO(source))
    principal = (tmp_path / "principal.xml").as_uri()
    result = compiled.transform(output_uri=principal)
    assert policy.calls == [(principal, True), (target.as_uri(), False)]
    assert target.as_uri() in result.secondary
    assert not target.exists()


def test_invalid_output_policy_is_rejected() -> None:
    with pytest.raises(TypeError, match="authorize"):
        XsltProcessor(output_policy=object())  # type: ignore[arg-type]


def test_output_policy_is_public() -> None:
    assert OutputPolicy.__name__ == "OutputPolicy"
