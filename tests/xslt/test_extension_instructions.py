"""The private Python-rule invocation instruction and its static checks.

``urn:pyxsd:python-rules:invoke`` is an engine-owned instruction.  It is only
legal inside the generated synthetic customization layer, is recognized before
literal-result handling, validates its rule id against the bound registry, and
executes one callback through :mod:`pyxsd.xslt.python_rules`.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

import pyxsd.xslt.processor as processor_module
from pyxsd.xslt import PythonRules, XsltExtensions, XsltProcessor
from pyxsd.xslt.errors import XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
APP = "urn:example:app"
PY = "urn:pyxsd:python-rules"

TARGET = '<xsl:template name="app:target" visibility="public"><original/></xsl:template>'


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


def compile_generated_layer(
    source: bytes,
    rules: PythonRules,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> Any:
    """Compile a real synthetic layer whose generated text is altered by *mutate*."""
    original = processor_module.build_python_layer

    def build(*args: Any, **kwargs: Any) -> str:
        return mutate(original(*args, **kwargs))

    monkeypatch.setattr(processor_module, "build_python_layer", build)
    return compile_rules(source, rules)


def code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def target_rules(callback: Any | None = None) -> PythonRules:
    return PythonRules.empty().override_template(
        "app:target",
        lambda context: "invoked" if callback is None else callback(context),
    )


def test_python_invoke_is_rejected_in_a_user_stylesheet() -> None:
    bodies = (
        '<xsl:template name="app:start"><py:invoke rule="0"/></xsl:template>',
        '<xsl:template name="app:start"><out><py:invoke rule="0"/></out></xsl:template>',
        '<xsl:template name="app:start"><xsl:fallback><py:invoke rule="0"/></xsl:fallback>'
        "</xsl:template>",
    )
    for body in bodies:
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(stylesheet(TARGET + body), target_rules())
        assert code(caught.value) == "XTSE0010"
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(stylesheet(TARGET + body), PythonRules.empty())
        assert code(caught.value) == "XTSE0010"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace('rule="0"', 'rule="1"'),
        lambda text: text.replace('rule="0"', 'rule="x"'),
        lambda text: text.replace('rule="0"', 'rule="-1"'),
        lambda text: text.replace('rule="0"', 'rule="1.5"'),
        lambda text: text.replace('<py:invoke rule="0"/>', "<py:invoke/>"),
    ],
)
def test_generated_layer_rule_ids_are_validated(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    source = stylesheet(TARGET)
    with pytest.raises(XsltStaticError) as caught:
        compile_generated_layer(source, target_rules(), monkeypatch, mutate)
    assert code(caught.value) == "XTSE0010"


def test_generated_layer_executes_the_bound_callback() -> None:
    def callback(context: Any) -> str:
        assert context.parameters == {}
        return "invoked"

    source = stylesheet(TARGET)
    compiled = compile_rules(source, target_rules(callback))
    assert (
        compiled.transform(initial_template="Q{urn:example:app}target")
        .serialize()
        .endswith("invoked")
    )


def test_user_stylesheet_cannot_spoof_the_generated_layer_marker() -> None:
    spoofed = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:app="{APP}" '
        f'xmlns:py="{PY}" py:layer="true">'
        f"{TARGET}"
        '<xsl:template name="app:start"><py:invoke rule="0"/></xsl:template>'
        "</xsl:stylesheet>"
    ).encode()
    with pytest.raises(XsltStaticError) as caught:
        compile_rules(spoofed, target_rules())
    assert code(caught.value) == "XTSE0010"
