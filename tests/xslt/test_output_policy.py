"""Application authorization of stylesheet-selected output destinations."""

import io

import pytest
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xslt import OutputPolicy
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.serialization import Serializer

OUT_URI = "https://example.test/out.xml"
SECONDARY_URI = "https://example.test/secondary.xml"
EXPECTED_PRINCIPAL_BYTES = b'<?xml version="1.0" encoding="UTF-8"?><primary/>'
EXPECTED_SECONDARY_BYTES = b'<?xml version="1.0" encoding="UTF-8"?><secondary/>'
OSERROR = OSError("destination unavailable")


class RecordingPolicy:
    """Record every authorization and optionally deny later writes."""

    def __init__(self) -> None:
        self.authorizations: list[tuple[str, bool]] = []
        self.deny = False

    def authorize(self, uri: str, *, principal: bool) -> None:
        self.authorizations.append((uri, principal))
        if self.deny:
            raise ResourcePolicyError("write denied")


class FailingDestination:
    """A binary destination whose write fails at the operating-system boundary."""

    def write(self, data: bytes) -> None:
        raise OSERROR


def _transform_pair(policy: OutputPolicy | None = None):
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}"><secondary/></xsl:result-document>'
        "<primary/></xsl:template>",
        attributes=' exclude-result-prefixes="xs"',
    )
    compiled = XsltProcessor(output_policy=policy).compile(io.BytesIO(source))
    return compiled.transform(output_uri=OUT_URI)


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


# ---------------------------------------------------------------------------
# Explicit result serialization and writing


def test_result_serialize_is_explicit_and_does_not_authorize(monkeypatch) -> None:
    def unexpected_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("serialize() wrote a result")

    monkeypatch.setattr(Serializer, "write", unexpected_write)
    policy = RecordingPolicy()
    result = _transform_pair(policy)
    policy.authorizations.clear()

    assert result.serialize() == EXPECTED_PRINCIPAL_BYTES.decode()
    assert result.serialize(SECONDARY_URI) == EXPECTED_SECONDARY_BYTES.decode()
    assert result.serialize(parameters={"omit-xml-declaration": True}) == "<primary/>"
    assert result.serialize(SECONDARY_URI, parameters={"omit-xml-declaration": True}) == (
        "<secondary/>"
    )
    assert policy.authorizations == []


def test_result_write_is_explicit_and_policy_authorized() -> None:
    policy = RecordingPolicy()
    result = _transform_pair(policy)
    policy.authorizations.clear()

    destination = io.BytesIO()
    result.write(destination)

    assert destination.getvalue() == EXPECTED_PRINCIPAL_BYTES
    assert policy.authorizations == [(OUT_URI, True)]


def test_result_write_selects_a_secondary_result() -> None:
    policy = RecordingPolicy()
    result = _transform_pair(policy)
    policy.authorizations.clear()

    destination = io.BytesIO()
    result.write(destination, SECONDARY_URI)

    assert destination.getvalue() == EXPECTED_SECONDARY_BYTES
    assert policy.authorizations == [(SECONDARY_URI, False)]


def test_result_write_without_a_policy_uses_parameter_overrides() -> None:
    result = _transform_pair()

    destination = io.BytesIO()
    result.write(destination, parameters={"omit-xml-declaration": True})

    assert destination.getvalue() == b"<primary/>"


def test_result_write_preserves_destination_failure(tmp_path) -> None:
    result = _transform_pair()

    with pytest.raises(XsltDynamicError) as caught:
        result.write(FailingDestination())
    assert caught.value.code is None
    assert caught.value.cause is OSERROR
    assert caught.value.__cause__ is OSERROR

    missing = tmp_path / "missing" / "out.xml"
    with pytest.raises(XsltDynamicError) as path_caught:
        result.write(missing)
    assert path_caught.value.code is None
    assert isinstance(path_caught.value.cause, FileNotFoundError)
    assert path_caught.value.__cause__ is path_caught.value.cause


def test_result_write_requires_an_absolute_output_identity() -> None:
    policy = RecordingPolicy()
    source = stylesheet(
        '<xsl:template name="xsl:initial-template"><primary/></xsl:template>',
        attributes=' exclude-result-prefixes="xs"',
    )
    result = XsltProcessor(output_policy=policy).compile(io.BytesIO(source)).transform()

    with pytest.raises(XsltDynamicError) as caught:
        result.write(io.BytesIO())
    assert caught.value.code == "XTDE1480"
    assert policy.authorizations == []


def test_result_write_preserves_policy_denials() -> None:
    policy = RecordingPolicy()
    result = _transform_pair(policy)
    policy.deny = True

    with pytest.raises(ResourcePolicyError, match="write denied"):
        result.write(io.BytesIO())
    assert policy.authorizations[-1] == (OUT_URI, True)


# ---------------------------------------------------------------------------
# Final-destination collisions and missing bases


def test_missing_base_output_uri_is_a_dynamic_error() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="relative.xml"><out/></xsl:result-document>'
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "XTDE1480"


def test_duplicate_secondary_uris_are_rejected() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/dup.xml"><one/>'
        "</xsl:result-document>"
        '<xsl:result-document href="https://example.test/dup.xml"><two/>'
        "</xsl:result-document>"
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "XTDE1490"


def test_secondary_uri_colliding_with_the_principal_uri_is_rejected() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{OUT_URI}"><one/>'
        "</xsl:result-document>"
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))

    # An empty principal delivers no final result tree, so the result document
    # occupies the base output URI and the invocation succeeds.
    result = compiled.transform(output_uri=OUT_URI)
    assert result.principal == ()
    assert list(result.secondary) == [OUT_URI]

    # Raw delivery constructs no final result tree either, so an empty raw
    # principal leaves the URI free as well.
    raw = compiled.transform(output_uri=OUT_URI, delivery="raw")
    assert raw.principal == ()
    assert list(raw.secondary) == [OUT_URI]

    # Document delivery always constructs the principal tree, so the empty
    # principal claims the URI and the same stylesheet is a collision.
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(output_uri=OUT_URI, delivery="document")
    assert caught.value.code == "XTDE1490"

    populated = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{OUT_URI}"><one/>'
        "</xsl:result-document>"
        "<principal/></xsl:template>"
    )
    populated_compiled = XsltProcessor().compile(io.BytesIO(populated))
    with pytest.raises(XsltDynamicError) as caught:
        populated_compiled.transform(output_uri=OUT_URI)
    assert caught.value.code == "XTDE1490"
    with pytest.raises(XsltDynamicError) as caught:
        populated_compiled.transform(output_uri=OUT_URI, delivery="raw")
    assert caught.value.code == "XTDE1490"
