"""The private Python-rule invocation instruction and its static checks.

``urn:pyxsd:python-rules:invoke`` is an engine-owned instruction.  It is only
legal inside the generated synthetic customization layer, is recognized before
literal-result handling, validates its rule id against the bound registry, and
executes one callback through :mod:`pyxsd.xslt.python_rules`.

Application-registered instructions follow the same per-compilation and
per-artifact snapshots through :class:`~pyxsd.xslt.extensions.XsltExtensions`:
an ``app:mark`` element declared under ``extension-element-prefixes`` is an
instruction candidate that must be registered on the compilation, and stays a
plain literal result element when the prefix is not declared.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pytest
from tests.xslt.support import stylesheet

import pyxsd.xslt.processor as processor_module
from pyxsd.xdm import XdmAtomic
from pyxsd.xslt import (
    ExtensionInstruction,
    PythonRules,
    XsltExtensions,
    XsltProcessor,
)
from pyxsd.xslt.compiler.plans import _PythonInvokePlan
from pyxsd.xslt.compiler.python_instructions import compile_python_invoke
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.python_rules import (
    PYTHON_INVOKE,
    PYTHON_LAYER_IDENTITY,
    PYTHON_RULES_NS,
    execute_python_plan,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
APP = "urn:example:app"
PY = "urn:pyxsd:python-rules"

TARGET = '<xsl:template name="app:target" visibility="public"><original/></xsl:template>'

NAMESPACES = {"app": APP, "doc": "urn:example:doc", "py": PY}


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
            compile_rules(
                stylesheet(TARGET + body, namespaces=NAMESPACES, declare_xs=False), target_rules()
            )
        assert code(caught.value) == "XTSE0010"
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(
                stylesheet(TARGET + body, namespaces=NAMESPACES, declare_xs=False),
                PythonRules.empty(),
            )
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
    source = stylesheet(TARGET, namespaces=NAMESPACES, declare_xs=False)
    with pytest.raises(XsltStaticError) as caught:
        compile_generated_layer(source, target_rules(), monkeypatch, mutate)
    assert code(caught.value) == "XTSE0010"


def test_generated_layer_executes_the_bound_callback() -> None:
    def callback(context: Any) -> str:
        assert context.parameters == {}
        return "invoked"

    source = stylesheet(TARGET, namespaces=NAMESPACES, declare_xs=False)
    compiled = compile_rules(source, target_rules(callback))
    assert (
        compiled.transform(initial_template="Q{urn:example:app}target")
        .serialize()
        .endswith("invoked")
    )


def test_compiled_artifact_registers_the_python_family_per_artifact() -> None:
    compiled = compile_rules(
        stylesheet(TARGET, namespaces=NAMESPACES, declare_xs=False), target_rules()
    )
    assert compiled.plan_executors[_PythonInvokePlan] is execute_python_plan
    contexts = [
        component.context
        for component in compiled._components.values()
        if component.component.module == PYTHON_LAYER_IDENTITY
    ]
    assert contexts
    for context in contexts:
        assert context.instruction_compilers[PYTHON_INVOKE] is compile_python_invoke


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


# ---------------------------------------------------------------------------
# Application-registered extension instructions


MARK_NS = "urn:app"
MARK_NAME = "{urn:app}mark"


@dataclass(frozen=True, slots=True)
class _MarkPlan:
    """The application's own compiled plan for one ``app:mark`` instruction."""

    text: str


def compile_mark(instruction: Any, context: Any, module: Any) -> _MarkPlan:
    """Application-supplied compiler: one plan per ``app:mark`` element."""
    del context, module
    text = instruction.attribute("text")
    return _MarkPlan("marked" if text is None else text)


def execute_mark(plan: Any, invocation: Any, context: Any) -> Any:
    """Application-supplied executor: the plan's text as one string atomic."""
    del invocation, context
    return (XdmAtomic.string(plan.text),)


EXPECTED_MARK_RESULT = (XdmAtomic.string("marked"),)


def mark_stylesheet(body: str, *, prefixes: str | None = "app") -> bytes:
    """A stylesheet whose ``app:*`` elements are extension-instruction candidates."""
    declaration = "" if prefixes is None else f' extension-element-prefixes="{prefixes}"'
    return stylesheet(body, attributes=declaration, namespaces={"app": MARK_NS}, declare_xs=False)


MARK_DOCUMENT = (
    f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:app="{MARK_NS}" '
    'extension-element-prefixes="app">'
    '<xsl:template name="xsl:initial-template" visibility="public"><app:mark/></xsl:template>'
    "</xsl:stylesheet>"
).encode()

MARK_START = '<xsl:template name="app:start" visibility="public"><app:mark/></xsl:template>'


def compile_mark_stylesheet(extensions: XsltExtensions | None = None, source: bytes | None = None):
    """Compile the default ``app:mark`` stylesheet with an optional registry.

    The default source runs ``xsl:initial-template``, so a plain
    ``transform()`` invokes the instruction; *source* replaces it for the
    named-template and fallback cases.
    """
    return XsltProcessor().compile(
        io.BytesIO(MARK_DOCUMENT if source is None else source),
        extensions=extensions,
    )


def start(compiled: Any) -> Any:
    return compiled.transform(initial_template=f"Q{{{MARK_NS}}}start")


def test_registered_instruction_executes_without_global_leakage():
    extensions = XsltExtensions().register_instruction(
        "{urn:app}mark", compiler=compile_mark, executor=execute_mark
    )
    enabled = compile_mark_stylesheet(extensions=extensions)
    assert enabled.transform().principal == EXPECTED_MARK_RESULT
    with pytest.raises(XsltStaticError):
        compile_mark_stylesheet().transform()


def test_registered_instruction_takes_its_attributes_from_the_element():
    extensions = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    source = mark_stylesheet(
        '<xsl:template name="app:start" visibility="public">'
        '<app:mark text="stamped"/></xsl:template>'
    )
    assert (
        start(compile_mark_stylesheet(extensions=extensions, source=source))
        .serialize()
        .endswith("stamped")
    )


def test_unavailable_instruction_runs_its_fallback():
    source = mark_stylesheet(
        '<xsl:template name="app:start" visibility="public">'
        "<app:mark><xsl:fallback>fallback-ran</xsl:fallback></app:mark></xsl:template>"
    )
    assert start(compile_mark_stylesheet(source=source)).serialize().endswith("fallback-ran")


def test_late_registration_cannot_change_a_compiled_artifact():
    source = mark_stylesheet(
        '<xsl:template name="app:start" visibility="public">'
        "<app:mark><xsl:fallback>fallback-ran</xsl:fallback></app:mark></xsl:template>"
    )
    compiled = compile_mark_stylesheet(source=source)
    assert start(compiled).serialize().endswith("fallback-ran")
    late = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    assert start(compiled).serialize().endswith("fallback-ran")
    enabled = compile_mark_stylesheet(extensions=late, source=source)
    assert start(enabled).serialize().endswith("marked")


def test_undeclared_foreign_element_stays_a_literal_result():
    source = mark_stylesheet(MARK_START, prefixes=None)
    literal = start(compile_mark_stylesheet(source=source))
    assert literal.serialize().endswith('<app:mark xmlns:app="urn:app"/>')


def test_registered_instruction_dispatches_even_when_undeclared():
    # Adjudicated deviation: the registry is the trust seam (XSLT 3.0 §18.3-lite;
    # the registrar is the compiling application), so a registered foreign
    # instruction is compiled before literal-result handling, declared or not.
    source = mark_stylesheet(MARK_START, prefixes=None)
    extensions = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    enabled = compile_mark_stylesheet(extensions=extensions, source=source)
    assert start(enabled).principal == EXPECTED_MARK_RESULT


def test_duplicate_instruction_registration_is_rejected():
    extensions = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    with pytest.raises(ValueError) as caught:
        extensions.register_instruction(MARK_NAME, compiler=compile_mark, executor=execute_mark)
    assert MARK_NAME in str(caught.value)


@pytest.mark.parametrize(
    ("name", "compiler", "executor"),
    [
        (42, compile_mark, execute_mark),
        ("", compile_mark, execute_mark),
        ("mark", compile_mark, execute_mark),
        ("app:mark", compile_mark, execute_mark),
        (MARK_NAME, "not-callable", execute_mark),
        (MARK_NAME, compile_mark, "not-callable"),
    ],
)
def test_instruction_registration_validates_names_and_callables(
    name: Any, compiler: Any, executor: Any
) -> None:
    with pytest.raises((TypeError, ValueError)):
        XsltExtensions().register_instruction(name, compiler=compiler, executor=executor)


def test_instruction_registration_normalises_an_eqname():
    extensions = XsltExtensions().register_instruction(
        f"Q{MARK_NAME}", compiler=compile_mark, executor=execute_mark
    )
    assert list(extensions.instructions) == [MARK_NAME]


def test_the_private_python_instruction_namespace_is_not_registerable():
    with pytest.raises(ValueError) as caught:
        XsltExtensions().register_instruction(
            PYTHON_INVOKE, compiler=compile_mark, executor=execute_mark
        )
    assert PYTHON_RULES_NS in str(caught.value)


def test_the_instructions_mapping_is_frozen():
    extensions = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    with pytest.raises(TypeError):
        extensions.instructions[MARK_NAME] = extensions.instructions[MARK_NAME]  # type: ignore[index]


def test_the_instruction_registry_validates_its_construction():
    registered = ExtensionInstruction(name=MARK_NAME, compiler=compile_mark, executor=execute_mark)
    with pytest.raises(TypeError):
        XsltExtensions(_instructions={MARK_NAME: "not-instruction"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        XsltExtensions(_instructions={MARK_NAME: registered, "wrong": registered})


def test_undeclared_default_namespace_keeps_foreign_elements_literal():
    source = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:app="{MARK_NS}" '
        'extension-element-prefixes="#default">'
        '<xsl:template name="xsl:initial-template" visibility="public"><app:thing/></xsl:template>'
        "</xsl:stylesheet>"
    ).encode()
    result = XsltProcessor().compile(io.BytesIO(source)).transform()
    assert result.serialize().endswith('<app:thing xmlns:app="urn:app"/>')


def test_unbound_extension_prefix_is_a_static_error():
    source = mark_stylesheet(
        '<xsl:template name="app:start" visibility="public"><app:mark/></xsl:template>',
        prefixes="missing",
    )
    with pytest.raises(XsltStaticError) as caught:
        compile_mark_stylesheet(source=source)
    assert code(caught.value) == "XTSE0280"


def test_default_extension_prefix_resolves():
    source = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:app="{MARK_NS}" xmlns="{MARK_NS}" extension-element-prefixes="#default">'
        f"{MARK_START}</xsl:stylesheet>"
    ).encode()
    with pytest.raises(XsltStaticError):
        compile_mark_stylesheet(source=source)
    extensions = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    assert (
        start(compile_mark_stylesheet(extensions=extensions, source=source))
        .serialize()
        .endswith("marked")
    )


def test_compiled_artifact_carries_the_application_snapshots():
    extensions = XsltExtensions().register_instruction(
        MARK_NAME, compiler=compile_mark, executor=execute_mark
    )
    compiled = compile_mark_stylesheet(extensions=extensions)
    assert compiled.plan_executors[_MarkPlan] is execute_mark
    carriers = [
        component.context
        for component in compiled._components.values()
        if MARK_NAME in component.context.instruction_compilers
    ]
    assert carriers
    for context in carriers:
        assert callable(context.instruction_compilers[MARK_NAME])
    unregistered = compile_mark_stylesheet(source=mark_stylesheet(MARK_START, prefixes=None))
    assert _MarkPlan not in unregistered.plan_executors
