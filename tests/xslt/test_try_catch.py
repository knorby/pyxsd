"""Executable ``xsl:try``/``xsl:catch``, coded selection, and output rollback.

The stylesheets here are inline and corpus-independent.  They cover the
transactional boundary between a failed try body and its catch handler:
principal output produced speculatively by the body is discarded, secondary
result entries are rolled back when recovery is enabled, catch bodies run with
only the outer lexical scope plus the six standard error variables, and a
catch-body failure is offered to an enclosing try rather than to a sibling
catch.  Python callback failures are deliberately outside this boundary and
keep their identity and their external side effects.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt import ExecutionLimits, XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.runtime.builder import ResultBuilder
from pyxsd.xslt.runtime.frames import CatchResult, Frame, Raise, Scheduler
from pyxsd.xslt.runtime.transformation import Transformation

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
ERR_NS = "http://www.w3.org/2005/xqt-errors"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"
CUSTOM_NS = "urn:custom"


def stylesheet(body: str, *, version: str = "3.0", attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:err="{ERR_NS}" '
        f'xmlns:xs="{XSD_NS}" xmlns:my="{MY_NS}" version="{version}"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def compile_stylesheet(
    body: str,
    *,
    version: str = "3.0",
    attributes: str = "",
    processor: XsltProcessor | None = None,
) -> Any:
    active = XsltProcessor() if processor is None else processor
    return active.compile(io.BytesIO(stylesheet(body, version=version, attributes=attributes)))


def transform(
    body: str,
    *,
    source: bytes = b"<doc/>",
    delivery: str = "document",
    limits: ExecutionLimits | None = None,
    parameters: Mapping[str, Any] | None = None,
    initial_template: str = "main",
    processor: XsltProcessor | None = None,
    version: str = "3.0",
    attributes: str = "",
) -> Any:
    compiled = compile_stylesheet(
        body,
        version=version,
        attributes=attributes,
        processor=processor,
    )
    return compiled.transform(
        source=source,
        initial_template=initial_template,
        delivery=delivery,
        limits=limits,
        parameters=parameters,
    )


def element(result: Any, name: str) -> XdmNode:
    for child in result.principal_tree.children:
        if child.kind is NodeKind.ELEMENT and child.local_name == name:
            return child
    raise AssertionError(f"no principal element named {name!r}")


def child_text(node: XdmNode, name: str) -> str:
    for child in node.children:
        if child.kind is NodeKind.ELEMENT and child.local_name == name:
            return child.string_value
    raise AssertionError(f"no child element named {name!r}")


# ---------------------------------------------------------------------------
# Rollback of speculative principal output


def test_catch_rolls_back_speculative_output() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:text>before,</xsl:text>
          <xsl:try>
            <speculative>speculative</speculative>
            <xsl:sequence select="1 div 0"/>
            <xsl:catch>
              <xsl:text>caught,</xsl:text>
            </xsl:catch>
          </xsl:try>
          <xsl:text>after</xsl:text>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "before,caught,after"
    assert "speculative" not in result.principal_tree.string_value


def test_catch_rolls_back_speculative_output_inside_literal_content() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:text>before,</xsl:text>
            <xsl:try>
              <speculative>speculative</speculative>
              <xsl:sequence select="1 div 0"/>
              <xsl:catch>
                <xsl:text>caught,</xsl:text>
              </xsl:catch>
            </xsl:try>
            <xsl:text>after</xsl:text>
          </out>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "before,caught,after"
    assert "speculative" not in result.principal_tree.string_value


# ---------------------------------------------------------------------------
# Coded catch selection


def test_specific_error_code_beats_wildcard() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="err:FOAR0001" select="'specific'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("specific"),)


def test_first_matching_catch_wins() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="err:XTDE0001" select="'first'"/>
            <xsl:catch errors="err:FOAR0001" select="'second'"/>
            <xsl:catch errors="*" select="'third'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("second"),)


def test_wildcard_and_standard_namespace_catches() -> None:
    wildcard = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="err:*" select="'standard'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )
    generic = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert wildcard.principal == (XdmAtomic.string("standard"),)
    assert generic.principal == (XdmAtomic.string("wildcard"),)


def test_expanded_custom_and_no_namespace_codes() -> None:
    expanded = transform(
        """
        <xsl:template name="main">
          <xsl:try select="error(QName('urn:custom', 'E1'))">
            <xsl:catch errors="Q{urn:custom}E1" select="'expanded'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )
    local = transform(
        """
        <xsl:template name="main">
          <xsl:try select="error(QName('', 'too-late'))">
            <xsl:catch errors="too-late" select="'local'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert expanded.principal == (XdmAtomic.string("expanded"),)
    assert local.principal == (XdmAtomic.string("local"),)


def test_any_namespace_local_name_test() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="error(QName('urn:custom', 'E1'))">
            <xsl:catch errors="*:E1" select="'any-namespace'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("any-namespace"),)


def test_unprefixed_standard_code_does_not_match() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="FOAR0001" select="'wrong'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("wildcard"),)


def test_empty_errors_attribute_catches_nothing() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="" select="'empty'"/>
            <xsl:catch errors="*" select="'wildcard'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("wildcard"),)


def test_empty_catch_catches_everything() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch select="'empty-catch'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("empty-catch"),)


def test_unmatched_catch_keeps_the_original_error_identity() -> None:
    with pytest.raises(Exception) as caught:
        transform(
            """
            <xsl:template name="main">
              <xsl:try select="1 div 0">
                <xsl:catch errors="err:XTDE0001" select="'wrong'"/>
              </xsl:try>
            </xsl:template>
            """
        )

    assert getattr(caught.value, "code", None) == "err:FOAR0001"


# ---------------------------------------------------------------------------
# Error variables


def test_error_variables_expose_code_description_and_value() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="error(QName('urn:custom', 'E1'), 'description text', (1, 2))">
            <xsl:catch errors="Q{urn:custom}E1">
              <out>
                <code-namespace>
                  <xsl:value-of select="namespace-uri-from-QName($err:code)"/>
                </code-namespace>
                <code-local>
                  <xsl:value-of select="local-name-from-QName($err:code)"/>
                </code-local>
                <description><xsl:value-of select="$err:description"/></description>
                <value-count><xsl:value-of select="count($err:value)"/></value-count>
                <value-items><xsl:value-of select="$err:value" separator=","/></value-items>
                <module-count><xsl:value-of select="count($err:module)"/></module-count>
                <line-count><xsl:value-of select="count($err:line-number)"/></line-count>
                <column-count><xsl:value-of select="count($err:column-number)"/></column-count>
              </out>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    out = element(result, "out")
    assert child_text(out, "code-namespace") == CUSTOM_NS
    assert child_text(out, "code-local") == "E1"
    assert child_text(out, "description") == "description text"
    assert child_text(out, "value-count") == "2"
    assert child_text(out, "value-items") == "1,2"
    assert child_text(out, "module-count") == "0"
    assert child_text(out, "line-count") == "0"
    assert child_text(out, "column-count") == "0"


def test_error_variables_for_a_no_description_error() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="error(QName('urn:custom', 'E2'))">
            <xsl:catch errors="Q{urn:custom}E2">
              <out>
                <description-count>
                  <xsl:value-of select="count($err:description)"/>
                </description-count>
                <code-local>
                  <xsl:value-of select="local-name-from-QName($err:code)"/>
                </code-local>
              </out>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    out = element(result, "out")
    assert child_text(out, "description-count") == "0"
    assert child_text(out, "code-local") == "E2"


@pytest.mark.parametrize(
    ("expression", "errors", "expected"),
    [
        ("error()", "err:FOER0000", "no-args"),
        ("error(())", "err:FOER0000", "empty-code"),
        ("error('not-a-qname')", "err:XPTY0004", "not-qname"),
        (
            "error((QName('urn:custom', 'E1'), QName('urn:custom', 'E2')))",
            "err:XPTY0004",
            "many-codes",
        ),
        (
            "error(QName('urn:custom', 'E1'), (1, 2))",
            "err:XPTY0004",
            "many-descriptions",
        ),
        (
            "error(QName('urn:custom', 'E1'), ())",
            "Q{urn:custom}E1",
            "empty-description",
        ),
    ],
)
def test_error_function_argument_forms(
    expression: str,
    errors: str,
    expected: str,
) -> None:
    result = transform(
        f"""
        <xsl:template name="main">
          <xsl:try select="{expression}">
            <xsl:catch errors="{errors}" select="'{expected}'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string(expected),)


def test_error_qname_falls_back_for_non_ncname_codes() -> None:
    from pyxsd.xslt.processor import _error_qname

    typed = _error_qname("{urn:custom}E1")
    fallback = _error_qname("1bad-code")

    assert typed.type_name.endswith("}QName")
    assert typed.value.local_name == "E1"
    assert fallback == XdmAtomic.string("1bad-code")


def test_assertion_error_value_and_module() -> None:
    processor = XsltProcessor(static_options={"assertions": True})
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:assert test="false()">
              <msg>boom</msg>
            </xsl:assert>
            <xsl:catch errors="err:XTMM9001">
              <out>
                <module-count>
                  <xsl:value-of select="count($err:module)"/>
                </module-count>
                <value-count>
                  <xsl:value-of select="count($err:value)"/>
                </value-count>
                <value-text>
                  <xsl:value-of select="$err:value/msg"/>
                </value-text>
              </out>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """,
        processor=processor,
    )

    out = element(result, "out")
    assert child_text(out, "module-count") == "1"
    assert child_text(out, "value-count") == "1"
    assert child_text(out, "value-text") == "boom"


# ---------------------------------------------------------------------------
# Try/catch forms and static validation


def test_try_select_and_catch_select() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="10 div 0">
            <xsl:catch errors="err:FOAR0001" select="'caught', 42"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("caught"), XdmAtomic.integer(42))


def test_try_select_inside_literal_content() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:try select="1 div 0">
              <xsl:catch errors="err:FOAR0001" select="'caught'"/>
            </xsl:try>
          </out>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "caught"


def test_successful_try_inside_literal_content() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:try select="'ok'">
              <xsl:catch select="'caught'"/>
            </xsl:try>
          </out>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "ok"


def test_successful_try_inside_literal_content_without_rollback() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:try rollback-output="no" select="'ok'">
              <xsl:catch select="'caught'"/>
            </xsl:try>
          </out>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "ok"


def test_caught_try_inside_literal_content_without_rollback() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:try rollback-output="no" select="1 div 0">
              <xsl:catch errors="err:FOAR0001" select="'caught'"/>
            </xsl:try>
          </out>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "caught"


def test_non_catchable_error_inside_literal_content_without_rollback() -> None:
    policy = _ExplodingPolicy()
    with pytest.raises(RuntimeError, match="python callback exploded"):
        transform(
            """
            <xsl:template name="main">
              <out>
                <xsl:try rollback-output="no">
                  <xsl:source-document href="urn:test:boom"/>
                  <xsl:catch select="'caught'"/>
                </xsl:try>
              </out>
            </xsl:template>
            """,
            processor=XsltProcessor(resource_policy=policy),
        )

    assert policy.calls == ["urn:test:boom"]


def test_non_catchable_error_inside_literal_content_is_not_caught() -> None:
    policy = _ExplodingPolicy()
    with pytest.raises(RuntimeError, match="python callback exploded"):
        transform(
            """
            <xsl:template name="main">
              <out>
                <xsl:try>
                  <xsl:source-document href="urn:test:boom"/>
                  <xsl:catch select="'caught'"/>
                </xsl:try>
              </out>
            </xsl:template>
            """,
            processor=XsltProcessor(resource_policy=policy),
        )

    assert policy.calls == ["urn:test:boom"]


def test_try_without_body_or_select_returns_empty() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:catch select="'unused'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == ()


def test_fallback_children_of_try_are_ignored() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:sequence select="2 + 2"/>
            <xsl:catch errors="*"/>
            <xsl:fallback>
              <xsl:sequence select="2 + 3"/>
            </xsl:fallback>
            <xsl:catch errors="*"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.integer(4),)


def test_try_select_with_content_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:try select="1">
                <xsl:text>content</xsl:text>
                <xsl:catch/>
              </xsl:try>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE3140"


def test_catch_select_with_content_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:sequence select="1"/>
                <xsl:catch select="'x'">
                  <xsl:text>content</xsl:text>
                </xsl:catch>
              </xsl:try>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE3150"


def test_catch_outside_try_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:catch/>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE0010"


def test_try_without_catch_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:text>content</xsl:text>
              </xsl:try>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE0010"


def test_invalid_rollback_output_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:try rollback-output="maybe">
                <xsl:sequence select="1"/>
                <xsl:catch/>
              </xsl:try>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE0020"


def test_literal_text_in_try_body_is_content() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            body-text
            <xsl:sequence select="1 div 0"/>
            <xsl:catch select="'caught'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("caught"),)


def test_whitespace_text_after_catch_is_ignored_by_the_compiler() -> None:
    from pyxsd.xslt.compiler.context import StaticContext
    from pyxsd.xslt.compiler.instructions import Instruction, TextNode
    from pyxsd.xslt.processor import _compile_try

    catch = Instruction(name=f"{{{XSLT_NS}}}catch")
    instruction = Instruction(
        name=f"{{{XSLT_NS}}}try",
        children=(TextNode("body"), catch, TextNode("   ")),
    )

    plan = _compile_try(instruction, StaticContext(), None)  # type: ignore[arg-type]

    assert len(plan.children) == 1
    assert len(plan.catches) == 1


def test_literal_text_after_catch_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:sequence select="1"/>
                <xsl:catch/>
                trailing text
              </xsl:try>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE0010"


def test_content_after_catch_is_a_static_error() -> None:
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:sequence select="1"/>
                <xsl:catch/>
                <xsl:text>content</xsl:text>
              </xsl:try>
            </xsl:template>
            """
        )

    assert caught.value.code == "XTSE0010"


# ---------------------------------------------------------------------------
# Nested handlers and catch-body failures


def test_nested_catch_handles_inner_error() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:try select="1 div 0">
              <xsl:catch errors="err:FOAR0001" select="'inner'"/>
            </xsl:try>
            <xsl:catch select="'outer'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("inner"),)


def test_catch_body_error_is_caught_by_outer_try() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:try select="1 div 0">
              <xsl:catch errors="err:FOAR0001">
                <xsl:sequence select="error(QName('urn:custom', 'E2'), 'catch failed')"/>
              </xsl:catch>
            </xsl:try>
            <xsl:catch errors="Q{urn:custom}E2" select="'outer'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("outer"),)


def test_sibling_catch_does_not_catch_catch_body_error() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:try select="1 div 0">
              <xsl:catch errors="err:FOAR0001">
                <xsl:sequence select="error(QName('urn:custom', 'E2'), 'catch failed')"/>
              </xsl:catch>
              <xsl:catch errors="Q{urn:custom}E2" select="'sibling'"/>
            </xsl:try>
            <xsl:catch errors="Q{urn:custom}E2" select="'outer'"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("outer"),)


def test_nested_try_inside_catch_body() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try select="1 div 0">
            <xsl:catch errors="err:FOAR0001">
              <xsl:try select="error(QName('urn:custom', 'E3'))">
                <xsl:catch errors="Q{urn:custom}E3" select="'nested-in-catch'"/>
              </xsl:try>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("nested-in-catch"),)


def test_outer_variables_are_visible_in_catch() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:variable name="outer" select="'outer-value'"/>
          <xsl:try select="1 div 0">
            <xsl:catch select="$outer"/>
          </xsl:try>
        </xsl:template>
        """,
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.string("outer-value"),)


def test_try_body_variables_are_not_visible_in_catch_body() -> None:
    with pytest.raises(Exception) as caught:
        transform(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:variable name="pi" select="3.14159"/>
                <xsl:sequence select="1 div 0"/>
                <xsl:catch>
                  <xsl:value-of select="$pi"/>
                </xsl:catch>
              </xsl:try>
            </xsl:template>
            """
        )

    assert getattr(caught.value, "code", None) == "XPST0008"


def test_try_body_variables_are_not_visible_in_catch_select() -> None:
    with pytest.raises(Exception) as caught:
        transform(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:variable name="pi" select="3.14159"/>
                <xsl:sequence select="1 div 0"/>
                <xsl:catch select="$pi"/>
              </xsl:try>
            </xsl:template>
            """
        )

    assert getattr(caught.value, "code", None) == "XPST0008"


# ---------------------------------------------------------------------------
# Secondary output and rollback-output


def test_secondary_output_is_rolled_back_by_default() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:result-document href="urn:test:speculative">
              <speculative/>
            </xsl:result-document>
            <xsl:sequence select="1 div 0"/>
            <xsl:catch>
              <xsl:text>caught</xsl:text>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    assert "urn:test:speculative" not in result.secondary
    assert result.principal_tree.string_value == "caught"


def test_secondary_output_is_retained_when_rollback_is_disabled() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try rollback-output="no">
            <xsl:result-document href="urn:test:speculative">
              <speculative/>
            </xsl:result-document>
            <xsl:sequence select="1 div 0"/>
            <xsl:catch>
              <xsl:text>caught</xsl:text>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    assert "urn:test:speculative" in result.secondary
    assert result.principal_tree.string_value == "caught"


def test_secondary_output_from_catch_body_is_kept() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:sequence select="1 div 0"/>
            <xsl:catch>
              <xsl:result-document href="urn:test:recovered" build-tree="no">
                <recovered/>
              </xsl:result-document>
              <xsl:text>caught</xsl:text>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    assert "urn:test:recovered" in result.secondary
    assert result.secondary["urn:test:recovered"].tree is None
    assert result.principal_tree.string_value == "caught"


def test_failed_result_document_can_be_retried_after_catch() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:result-document href="urn:test:same">
              <xsl:sequence select="1 div 0"/>
            </xsl:result-document>
            <xsl:catch>
              <xsl:result-document href="urn:test:same" build-tree="yes">
                <recovered/>
              </xsl:result-document>
              <xsl:text>caught</xsl:text>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "caught"
    entry = result.secondary["urn:test:same"]
    assert entry.serialization["build-tree"] == "yes"
    tree = entry.tree
    assert tree is not None
    assert tree.children[0].local_name == "recovered"


def test_nested_secondary_output_rolls_back_outer_failure() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:try>
              <xsl:result-document href="urn:test:inner">
                <inner/>
              </xsl:result-document>
              <xsl:sequence select="1 div 0"/>
              <xsl:catch>
                <xsl:result-document href="urn:test:recovered">
                  <recovered/>
                </xsl:result-document>
                <xsl:text>inner-caught</xsl:text>
              </xsl:catch>
            </xsl:try>
            <xsl:sequence select="error(QName('urn:custom', 'E4'))"/>
            <xsl:catch>
              <xsl:text>outer-caught</xsl:text>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """
    )

    assert "urn:test:inner" not in result.secondary
    assert "urn:test:recovered" not in result.secondary
    assert result.principal_tree.string_value == "outer-caught"


def test_disabled_rollback_still_discards_speculative_principal_output() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:text>before,</xsl:text>
          <xsl:try rollback-output="no">
            <speculative>speculative</speculative>
            <xsl:sequence select="1 div 0"/>
            <xsl:catch>
              <xsl:text>caught,</xsl:text>
            </xsl:catch>
          </xsl:try>
          <xsl:text>after</xsl:text>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "before,caught,after"


# ---------------------------------------------------------------------------
# Tail behavior near handlers


def test_tail_recursion_inside_try_uses_frames() -> None:
    result = transform(
        """
        <xsl:template name="count">
          <xsl:param name="n" select="0"/>
          <xsl:try>
            <xsl:choose>
              <xsl:when test="$n = 0"><xsl:text>done</xsl:text></xsl:when>
              <xsl:otherwise>
                <xsl:call-template name="count">
                  <xsl:with-param name="n" select="$n - 1"/>
                </xsl:call-template>
              </xsl:otherwise>
            </xsl:choose>
            <xsl:catch select="'unexpected'"/>
          </xsl:try>
        </xsl:template>
        <xsl:template name="main">
          <xsl:call-template name="count">
            <xsl:with-param name="n" select="10000"/>
          </xsl:call-template>
        </xsl:template>
        """
    )

    assert result.principal_tree.string_value == "done"


def test_tail_call_in_catch_body_is_eliminated() -> None:
    result = transform(
        """
        <xsl:template name="count">
          <xsl:param name="n" select="0"/>
          <xsl:try>
            <xsl:sequence select="error(QName('urn:custom', 'E5'))"/>
            <xsl:catch errors="Q{urn:custom}E5">
              <xsl:choose>
                <xsl:when test="$n = 0"><xsl:text>done</xsl:text></xsl:when>
                <xsl:otherwise>
                  <xsl:call-template name="count">
                    <xsl:with-param name="n" select="$n - 1"/>
                  </xsl:call-template>
                </xsl:otherwise>
              </xsl:choose>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        <xsl:template name="main">
          <xsl:call-template name="count">
            <xsl:with-param name="n" select="500"/>
          </xsl:call-template>
        </xsl:template>
        """,
        limits=ExecutionLimits(max_frames=60),
    )

    assert result.principal_tree.string_value == "done"


# ---------------------------------------------------------------------------
# Element availability


def test_element_available_reports_try_and_catch() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out try="{element-available('xsl:try')}"
               catch="{element-available('xsl:catch')}"/>
        </xsl:template>
        """
    )

    out = element(result, "out")
    assert out.attribute("try") is not None
    assert out.attribute("try").string_value == "true"
    assert out.attribute("catch").string_value == "true"


def test_element_available_rejects_try_in_effective_20() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <out try="{element-available('xsl:try')}"/>
        </xsl:template>
        """,
        version="2.0",
    )

    out = element(result, "out")
    assert out.attribute("try").string_value == "false"


# ---------------------------------------------------------------------------
# External effects at the boundary


def _stylesheet_resource(source: Any) -> ResourceData | None:
    """Serve the in-memory stylesheet while leaving named resources to a policy."""
    if isinstance(source, str) and source.startswith("urn:test:"):
        return None
    if hasattr(source, "read"):
        content = source.read()
        if isinstance(content, str):
            content = content.encode()
        return ResourceData(source_id="<memory>:1", content=content)
    return None


class _ExplodingPolicy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        del base, purpose
        admitted = _stylesheet_resource(source)
        if admitted is not None:
            return admitted
        self.calls.append(source)
        raise RuntimeError("python callback exploded")


class _MissingPolicy:
    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        del base, purpose
        admitted = _stylesheet_resource(source)
        if admitted is not None:
            return admitted
        raise OSError(f"missing {source}")


def test_python_callback_exception_is_not_caught_and_side_effect_remains() -> None:
    policy = _ExplodingPolicy()
    with pytest.raises(RuntimeError, match="python callback exploded"):
        transform(
            """
            <xsl:template name="main">
              <xsl:try>
                <xsl:source-document href="urn:test:boom"/>
                <xsl:catch select="'caught'"/>
              </xsl:try>
            </xsl:template>
            """,
            processor=XsltProcessor(resource_policy=policy),
        )

    assert policy.calls == ["urn:test:boom"]


def test_converted_resource_error_is_caught() -> None:
    result = transform(
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:source-document href="urn:test:missing"/>
            <xsl:catch>
              <out>
                <code><xsl:value-of select="local-name-from-QName($err:code)"/></code>
                <description-count>
                  <xsl:value-of select="count($err:description)"/>
                </description-count>
              </out>
            </xsl:catch>
          </xsl:try>
        </xsl:template>
        """,
        processor=XsltProcessor(resource_policy=_MissingPolicy()),
    )

    out = element(result, "out")
    assert child_text(out, "code") == "FODC0002"
    assert child_text(out, "description-count") == "1"


# ---------------------------------------------------------------------------
# Transactional builder extents


def test_catch_result_validates_its_frame_and_value_forms() -> None:
    frame = Frame(kind="catch")
    assert CatchResult(frame=frame).frame is frame
    assert CatchResult(("value",)).value == ("value",)

    with pytest.raises(TypeError, match="frame"):
        CatchResult(frame=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="both"):
        CatchResult(("value",), frame=frame)


def test_builder_transaction_rolls_back_nested_construction() -> None:
    builder = ResultBuilder.document()
    with pytest.raises(XsltDynamicError), builder.transaction():
        root = builder.element("root")
        builder.attribute("id", "a")
        builder.namespace("p", "urn:p")
        builder.text("speculative")
        builder.element("child")
        builder.text("nested")
        builder.end_element()
        assert root.children
        raise XsltDynamicError("speculative failure", code="XTDE0001")

    assert builder.to_sequence() == ()
    assert builder.root.children == ()


def test_builder_extent_rolls_back_across_a_scheduler_frame() -> None:
    builder = ResultBuilder.document()
    extent = builder.transaction()

    def step(frame: Frame) -> Any:
        del frame
        builder.element("root")
        builder.attribute("id", "a")
        builder.text("speculative")
        return Raise(XsltDynamicError("speculative failure", code="XTDE0001"))

    def handler(error: BaseException, stack: tuple[Any, ...]) -> CatchResult:
        del stack
        assert getattr(error, "code", None) == "XTDE0001"
        return CatchResult(("caught",))

    scheduler = Scheduler()
    result = scheduler.run(
        Frame(kind="try", extent=extent, error_handler=handler),
        step,
    )

    assert result == ("caught",)
    assert extent.rolled_back
    assert builder.to_sequence() == ()


# ---------------------------------------------------------------------------
# Error object typing at the boundary


def test_error_metadata_is_typed_and_validated() -> None:
    error = XsltDynamicError(
        "typed",
        code="Q{urn:custom}E1",
        description="typed description",
        value=(XdmAtomic.integer(7),),
        line_number=12,
        column_number=3,
    )

    assert error.description == "typed description"
    assert error.has_description is True
    assert error.value == (XdmAtomic.integer(7),)
    assert error.line_number == 12
    assert error.column_number == 3

    with pytest.raises(TypeError, match="description"):
        XsltDynamicError("bad", description=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="value"):
        XsltDynamicError("bad", value=(object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="line_number"):
        XsltDynamicError("bad", line_number=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="column_number"):
        XsltDynamicError("bad", column_number="1")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="has_description"):
        XsltDynamicError("bad", has_description=1)  # type: ignore[arg-type]


def test_transformation_boundary_preserves_python_cause_identity() -> None:
    transformation = Transformation()
    original = RuntimeError("python callback exploded")

    wrapped = transformation.boundary_error(original, catchable=True)

    assert isinstance(wrapped, XsltDynamicError)
    assert wrapped.catchable is False
    assert wrapped.cause is original
    assert wrapped.__cause__ is original
    assert wrapped.description == "python callback exploded"
    assert wrapped.value == ()
