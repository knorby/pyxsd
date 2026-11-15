"""Compilers for the mandatory constructed-node instructions.

``xsl:comment``, ``xsl:processing-instruction``, and ``xsl:namespace`` each
construct one leaf node whose string value comes from either a ``select``
expression or a sequence constructor.  The family owns its compiler mapping so
the processor only merges the registration; the shared sequence-constructor
compiler is imported lazily because the processor imports this module to build
its instruction registry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.dispatch import InstructionCompiler
from pyxsd.xslt.compiler.expressions import compile_avt, compile_xpath
from pyxsd.xslt.compiler.instructions import Instruction, instruction_name
from pyxsd.xslt.compiler.plans import (
    _CommentPlan,
    _FallbackPlan,
    _NamespacePlan,
    _Plan,
    _ProcessingInstructionPlan,
    _TextPlan,
)
from pyxsd.xslt.compiler.static import EffectiveModule
from pyxsd.xslt.errors import XsltStaticError

__all__ = [
    "CONSTRUCTOR_INSTRUCTION_COMPILERS",
    "compile_comment",
    "compile_namespace",
    "compile_processing_instruction",
]


def _sequence(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> tuple[_Plan, ...]:
    """Compile the instruction's sequence constructor with the shared compiler."""
    from pyxsd.xslt.processor import _compile_sequence

    return _compile_sequence(instruction.children, context, module)


def _has_content(plans: Iterable[_Plan], *, allow_fallback: bool = False) -> bool:
    """Whether a compiled constructor carries content beyond whitespace.

    ``allow_fallback`` treats ``xsl:fallback`` children as absent, which is the
    content test ``xsl:namespace`` uses for its mutually exclusive select and
    sequence constructor.
    """
    for plan in plans:
        if allow_fallback and isinstance(plan, _FallbackPlan):
            continue
        if isinstance(plan, _TextPlan):
            if plan.value.strip():
                return True
            continue
        return True
    return False


def _select_error(
    instruction: Instruction,
    context: StaticContext,
    *,
    code: str,
    label: str,
) -> XsltStaticError:
    return XsltStaticError(
        f"{label} with a select attribute must have empty content",
        code=code,
        uri=context.uri,
        location=instruction.location,
    )


def compile_comment(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile one ``xsl:comment`` constructor."""
    select = instruction.attribute("select")
    children = _sequence(instruction, context, module)
    if select is not None and _has_content(children):
        raise _select_error(instruction, context, code="XTSE0940", label="xsl:comment")
    return _CommentPlan(
        instruction=instruction,
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


def compile_processing_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile one ``xsl:processing-instruction`` constructor."""
    name = instruction.attribute("name")
    if name is None:
        raise XsltStaticError(
            "xsl:processing-instruction requires a name attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    select = instruction.attribute("select")
    children = _sequence(instruction, context, module)
    if select is not None and _has_content(children):
        raise _select_error(
            instruction,
            context,
            code="XTSE0880",
            label="xsl:processing-instruction",
        )
    return _ProcessingInstructionPlan(
        instruction=instruction,
        name=compile_avt(name, context, location=instruction.location),
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


def compile_namespace(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile one ``xsl:namespace`` constructor."""
    name = instruction.attribute("name")
    if name is None:
        raise XsltStaticError(
            "xsl:namespace requires a name attribute",
            code="XTSE0010",
            uri=context.uri,
            location=instruction.location,
        )
    select = instruction.attribute("select")
    children = _sequence(instruction, context, module)
    has_content = _has_content(children, allow_fallback=True)
    if (select is not None and has_content) or (select is None and not has_content):
        raise XsltStaticError(
            "xsl:namespace requires exactly one of a select attribute or content",
            code="XTSE0910",
            uri=context.uri,
            location=instruction.location,
        )
    return _NamespacePlan(
        instruction=instruction,
        name=compile_avt(name, context, location=instruction.location),
        expression=(
            compile_xpath(select, context, location=instruction.location)
            if select is not None
            else None
        ),
        children=children,
    )


#: The constructed-node family's process-global compiler registration.
CONSTRUCTOR_INSTRUCTION_COMPILERS: Mapping[str, InstructionCompiler] = MappingProxyType(
    {
        instruction_name("comment"): compile_comment,
        instruction_name("processing-instruction"): compile_processing_instruction,
        instruction_name("namespace"): compile_namespace,
    }
)
