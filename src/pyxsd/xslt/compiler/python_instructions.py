"""Compiler for the engine-owned Python customization instructions.

``py:invoke`` is private to the synthetic customization layer the processor
generates from a :class:`~pyxsd.xslt.python_rules.PythonRules` registry.  The
family is registered on each compilation's static context instead of the
process-global instruction registry: the compiler itself gates the instruction
to the generated layer, so a user stylesheet that names ``py:invoke`` — even
inside literal result content — fails with ``XTSE0010`` before any rule is
bound, and an unregistered foreign element keeps its literal-result handling.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.dispatch import InstructionCompiler
from pyxsd.xslt.compiler.instructions import Instruction
from pyxsd.xslt.compiler.plans import _PythonInvokePlan
from pyxsd.xslt.compiler.static import EffectiveModule
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.python_rules import PYTHON_INVOKE, PYTHON_LAYER_IDENTITY

__all__ = [
    "PYTHON_INSTRUCTION_COMPILERS",
    "compile_python_invoke",
]


def compile_python_invoke(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule | None = None,
) -> _PythonInvokePlan:
    """Validate the private ``py:invoke`` rule id against the bound registry.

    *module* is the family-compiler protocol argument; the gate reads the
    instruction's own module identity because the instruction may only come
    from the generated layer, never from a compiled application module.
    """
    if instruction.module != PYTHON_LAYER_IDENTITY:
        raise XsltStaticError(
            "the Python rule instruction is only permitted in a generated layer",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    source = instruction.attribute("rule")
    if source is None or not source.strip():
        raise XsltStaticError(
            "the Python rule instruction requires a rule attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    try:
        rule = int(source)
    except ValueError as exc:
        raise XsltStaticError(
            f"the Python rule id {source!r} is not an integer",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
            cause=exc,
        ) from exc
    count = context.python_rule_count
    if rule < 0 or count is None or rule >= count:
        raise XsltStaticError(
            f"the Python rule id {rule} is not registered",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    return _PythonInvokePlan(rule)


#: The family's per-compilation registration, installed on every compilation
#: static context by the processor.  The mapping is an immutable snapshot; it
#: is never added to the process-global registry.
PYTHON_INSTRUCTION_COMPILERS: Mapping[str, InstructionCompiler] = MappingProxyType(
    {PYTHON_INVOKE: compile_python_invoke}
)
