"""Stylesheet message events are scoped to one transformation."""

import io

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.runtime.frames import ExecutionLimits


def test_message_is_reported_without_becoming_result_content() -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            "<xsl:message select=\"'hello'\"/>"
            "<out/>"
            "</xsl:template>"
        )
    )

    assert [message.text for message in result.messages] == ["hello"]
    assert result.principal_tree.root_element.local_name == "out"


def test_terminating_message_raises_dynamic_error() -> None:
    with pytest.raises(XsltDynamicError) as exc:
        transform(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                '<xsl:message terminate="yes">stop</xsl:message>'
                "</xsl:template>"
            )
        )
    assert exc.value.code == "XTMM9000"


def test_messages_are_not_shared_between_invocations() -> None:
    compiled = XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                "<xsl:message>one</xsl:message>"
                "</xsl:template>"
            )
        )
    )
    assert [message.text for message in compiled.transform().messages] == ["one"]
    assert [message.text for message in compiled.transform().messages] == ["one"]


def test_message_terminate_empty_is_invalid() -> None:
    with pytest.raises(XsltStaticError) as exc:
        transform(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                '<xsl:message terminate="">stop</xsl:message>'
                "</xsl:template>"
            )
        )
    assert exc.value.code == "XTSE0020"


def test_message_custom_error_code_is_reported() -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            '<xsl:message error-code="e:notice">hello</xsl:message>'
            "</xsl:template>",
            namespaces={"e": "urn:errors"},
        )
    )
    assert result.messages[0].code == "{urn:errors}notice"


@pytest.mark.parametrize("value", ["true", "1", " yes "])
def test_message_terminate_accepts_boolean_lexical_values(value: str) -> None:
    with pytest.raises(XsltDynamicError) as exc:
        transform(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                f'<xsl:message terminate="{value}">stop</xsl:message>'
                "</xsl:template>"
            )
        )
    assert exc.value.code == "XTMM9000"


def test_message_content_dynamic_error_does_not_fail_transformation() -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            '<xsl:message select="error()"/>'
            "<out/>"
            "</xsl:template>"
        )
    )
    assert result.principal_tree.root_element.local_name == "out"


def test_terminating_message_is_catchable_by_custom_error_code() -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            '<xsl:try><xsl:message terminate="yes" error-code="e:stop">stop</xsl:message>'
            '<xsl:catch errors="e:stop"><recovered/></xsl:catch></xsl:try>'
            "</xsl:template>",
            namespaces={"e": "urn:errors"},
        )
    )
    assert result.principal_tree.root_element.local_name == "recovered"
    assert result.messages[0].code == "{urn:errors}stop"
    assert result.messages[0].terminate is True


@pytest.mark.parametrize("value", ["no", "false", "0"])
def test_message_terminate_false_values_do_not_stop(value: str) -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            f'<xsl:message terminate="{value}">continue</xsl:message>'
            "</xsl:template>"
        )
    )
    assert result.messages[0].terminate is False


def test_invalid_message_error_code_uses_default() -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            '<xsl:message error-code="missing:prefix">hello</xsl:message>'
            "</xsl:template>"
        )
    )
    assert result.messages[0].code == "XTMM9000"


def test_message_rejects_unknown_unqualified_attribute() -> None:
    with pytest.raises(XsltStaticError) as exc:
        transform(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                '<xsl:message test="true()"/>'
                "</xsl:template>"
            )
        )
    assert exc.value.code == "XTSE0090"


def test_message_allows_extension_and_forward_compatible_attributes() -> None:
    result = transform(
        stylesheet(
            '<xsl:template name="xsl:initial-template">'
            '<xsl:message e:extra="allowed" future="allowed">hello</xsl:message>'
            "</xsl:template>",
            version="4.0",
            namespaces={"e": "urn:extension"},
        )
    )
    assert result.messages[0].text == "hello"


def test_message_does_not_swallow_execution_budget_errors() -> None:
    with pytest.raises(XsltDynamicError) as exc:
        transform(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                '<xsl:message><xsl:call-template name="recurse"/></xsl:message>'
                '</xsl:template><xsl:template name="recurse">'
                '<xsl:call-template name="recurse"/></xsl:template>'
            ),
            limits=ExecutionLimits(max_steps=30),
        )
    assert exc.value.catchable is False
