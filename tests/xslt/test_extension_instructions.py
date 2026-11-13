"""The private Python-rule invocation instruction and its static checks.

``urn:pyxsd:python-rules:invoke`` is an engine-owned instruction, never a
user-authored literal result element.  It is recognized before literal-result
handling, validates its rule id against the bound registry, and executes one
callback through :mod:`pyxsd.xslt.python_rules`.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xslt import PythonRules, XsltExtensions, XsltProcessor
from pyxsd.xslt.errors import XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
APP = "urn:example:app"
PY = "urn:pyxsd:python-rules"


def stylesheet(body: str) -> bytes:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:app="{APP}" xmlns:doc="urn:example:doc" xmlns:py="{PY}">'
        f"{body}</xsl:stylesheet>"
    ).encode()


def compile_rules(source: bytes, rules: PythonRules) -> Any:
    return XsltProcessor().compile(
        io.BytesIO(source),
        extensions=XsltExtensions().with_python_rules(rules),
    )


def code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


@pytest.mark.parametrize(
    "invoke",
    [
        '<py:invoke rule="0"/>',
        '<py:invoke rule="x"/>',
        '<py:invoke rule="-1"/>',
        '<py:invoke rule="1.5"/>',
        "<py:invoke/>",
    ],
)
def test_python_invoke_requires_a_bound_rule_id(invoke: str) -> None:
    source = stylesheet(f'<xsl:template name="app:start">{invoke}</xsl:template>')
    with pytest.raises(XsltStaticError) as caught:
        compile_rules(source, PythonRules.empty())
    assert code(caught.value) == "XTSE0010"


def test_python_invoke_checks_the_registry_bound() -> None:
    rules = PythonRules.empty().add_mode_rule("app:process", "doc:item", lambda context: ())
    mode = '<xsl:mode name="app:process" visibility="public"/>'
    valid = stylesheet(mode + '<xsl:template name="app:start"><py:invoke rule="0"/></xsl:template>')
    assert compile_rules(valid, rules) is not None

    out_of_range = stylesheet(
        mode + '<xsl:template name="app:start"><py:invoke rule="1"/></xsl:template>'
    )
    with pytest.raises(XsltStaticError) as caught:
        compile_rules(out_of_range, rules)
    assert code(caught.value) == "XTSE0010"


def test_python_invoke_is_not_literal_result_content_or_fallback() -> None:
    for body in (
        '<xsl:template name="app:start"><out><py:invoke rule="0"/></out></xsl:template>',
        '<xsl:template name="app:start"><xsl:fallback><py:invoke rule="0"/></xsl:fallback>'
        "</xsl:template>",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(stylesheet(body), PythonRules.empty())
        assert code(caught.value) == "XTSE0010"


def test_python_invoke_executes_the_bound_callback() -> None:
    def callback(context: Any) -> str:
        assert context.parameters == {}
        return "invoked"

    rules = PythonRules.empty().override_template("app:target", callback)
    source = stylesheet(
        '<xsl:template name="app:target"><unused/></xsl:template>'
        '<xsl:template name="app:start"><py:invoke rule="0"/></xsl:template>'
    )
    compiled = compile_rules(source, rules)
    assert (
        compiled.transform(initial_template="Q{urn:example:app}start")
        .serialize()
        .endswith("invoked")
    )
