"""Structural regression for extension-instruction dispatch.

The Python customization family must stay out of the processor: its compiler
lives in :mod:`pyxsd.xslt.compiler.python_instructions`, its executor in
:mod:`pyxsd.xslt.python_rules`, registration is an immutable per-compilation
snapshot on the static context, and execution is a per-artifact plan-executor
lookup.  No Python-instruction-specific branch may reappear in the processor.
"""

from __future__ import annotations

import ast
import inspect
import io
from typing import Any

import pytest

import pyxsd.xslt.processor as processor_module
import pyxsd.xslt.runtime.instructions as runtime_instructions
from pyxsd.xslt import PythonRules, XsltExtensions, XsltProcessor
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.dispatch import compile_instruction, supports_instruction
from pyxsd.xslt.compiler.instructions import Instruction
from pyxsd.xslt.compiler.plans import _PythonInvokePlan
from pyxsd.xslt.compiler.python_instructions import (
    PYTHON_INSTRUCTION_COMPILERS,
    compile_python_invoke,
)
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.python_rules import PYTHON_INVOKE, execute_python_plan, execute_python_rule
from pyxsd.xslt.runtime.dispatch import PYTHON_PLAN_EXECUTORS, execute_registered_plan

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
APP = "urn:example:app"
PY = "urn:pyxsd:python-rules"
APP_INVOKE = "{urn:example:app-foreign}invoke"

TARGET = '<xsl:template name="app:target" visibility="public"><original/></xsl:template>'

EXPECTED_OVERRIDE_RESULT = '<?xml version="1.0" encoding="UTF-8"?>overridden'


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


def target_rules() -> PythonRules:
    return PythonRules.empty().override_template("app:target", lambda context: "invoked")


def python_override_result() -> str:
    rules = PythonRules.empty().override_template("app:target", lambda context: "overridden")
    return (
        compile_rules(stylesheet(TARGET), rules)
        .transform(initial_template="Q{urn:example:app}target")
        .serialize()
    )


def compile_user_py_invoke() -> None:
    body = '<xsl:template name="app:start"><py:invoke rule="0"/></xsl:template>'
    compile_rules(stylesheet(TARGET + body), target_rules())


def test_python_instruction_has_dedicated_compiler_and_executor():
    assert compile_python_invoke.__module__ == "pyxsd.xslt.compiler.python_instructions"
    assert execute_python_rule.__module__ == "pyxsd.xslt.python_rules"
    assert not hasattr(processor_module, "_compile_python_invoke")
    assert not hasattr(runtime_instructions, "execute_python_invoke")


def test_python_layer_still_executes_and_user_spoof_is_rejected():
    assert python_override_result() == EXPECTED_OVERRIDE_RESULT
    with pytest.raises(XsltStaticError) as caught:
        compile_user_py_invoke()
    assert caught.value.code == "XTSE0010"


def test_the_python_family_is_registered_per_compilation_and_per_artifact() -> None:
    assert dict(PYTHON_INSTRUCTION_COMPILERS) == {PYTHON_INVOKE: compile_python_invoke}
    assert dict(PYTHON_PLAN_EXECUTORS) == {_PythonInvokePlan: execute_python_plan}


def test_processor_has_no_python_instruction_branch() -> None:
    source = inspect.getsource(processor_module)
    assert "PYTHON_INVOKE" not in source
    assert "_PythonInvokePlan" not in source
    assert "execute_python_invoke" not in source
    tree = ast.parse(source)
    functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert not any("python_invoke" in name for name in functions)


def test_dispatch_consults_per_compilation_compilers_in_addition_to_built_ins() -> None:
    sentinel = object()

    def compiler(instruction: Any, context: Any, module: Any) -> Any:
        return sentinel

    registered = StaticContext(instruction_compilers={APP_INVOKE: compiler})
    assert supports_instruction(APP_INVOKE, registered)
    assert not supports_instruction(APP_INVOKE, StaticContext())
    assert supports_instruction(f"{{{XSLT_NS}}}value-of", StaticContext())
    assert not supports_instruction("{urn:example:unknown}thing", StaticContext())
    assert compile_instruction(Instruction(name=APP_INVOKE), registered, None) is sentinel


def test_per_compilation_registration_overrides_a_built_in_family() -> None:
    sentinel = object()

    def compiler(instruction: Any, context: Any, module: Any) -> Any:
        return sentinel

    name = f"{{{XSLT_NS}}}text"
    context = StaticContext(instruction_compilers={name: compiler})
    assert compile_instruction(Instruction(name=name), context, None) is sentinel


def test_instruction_compilers_are_an_immutable_snapshot() -> None:
    def compiler(instruction: Any, context: Any, module: Any) -> Any:
        return ()

    context = StaticContext(instruction_compilers={APP_INVOKE: compiler})
    with pytest.raises(TypeError):
        context.instruction_compilers["{urn:example:other}invoke"] = compiler  # type: ignore[index]
    with pytest.raises(TypeError):
        StaticContext(instruction_compilers={5: compiler})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        StaticContext(instruction_compilers={APP_INVOKE: "compiler"})  # type: ignore[dict-item]


class _RegisteredPlan:
    def __init__(self, index: int) -> None:
        self.index = index


def test_registered_plan_execution_is_a_generic_artifact_lookup() -> None:
    seen: list[tuple[object, object, object]] = []

    def executor(plan: Any, invocation: Any, context: Any) -> Any:
        seen.append((plan, invocation, context))
        return ("executed",)

    executors = {_RegisteredPlan: executor}
    plan = _RegisteredPlan(7)
    assert execute_registered_plan(plan, "invocation", "context", executors) == ("executed",)
    assert seen == [(plan, "invocation", "context")]
    with pytest.raises(TypeError):
        execute_registered_plan(plan, "invocation", "context", {})
