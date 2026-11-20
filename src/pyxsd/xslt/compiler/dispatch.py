"""Name-keyed dispatch of normalized XSLT instructions to their compilers.

Each instruction family owns one dedicated compiler.  The registry is keyed by
the instruction's expanded XSLT name, so a normalized node selects its family
without a central conditional.  Literal result elements and unregistered
instructions stay outside the registry: literal construction is a distinct plan
shape, and forwards-compatible or unsupported instructions keep their explicit
handling in the instruction compiler.

Registration is declarative.  The compilation module installs every built-in
family compiler in one process-global mapping when it is imported.  A
compilation can carry additional family compilers on its static context — an
immutable per-compilation snapshot, never a process-global entry — and the
lookup consults that snapshot first, then the built-in registry.  Dispatch
itself only performs the expanded-name lookup and call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeAlias

from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.instructions import Instruction
from pyxsd.xslt.compiler.plans import _Plan
from pyxsd.xslt.compiler.static import EffectiveModule

__all__ = [
    "InstructionCompiler",
    "compile_instruction",
    "register_instruction_compilers",
    "supports_instruction",
]

InstructionCompiler: TypeAlias = Callable[
    [Instruction, StaticContext, EffectiveModule],
    _Plan,
]

_COMPILERS: dict[str, InstructionCompiler] = {}


def register_instruction_compilers(compilers: Mapping[str, InstructionCompiler]) -> None:
    """Install one compiler for each expanded XSLT instruction name."""
    _COMPILERS.update(compilers)


def supports_instruction(name: str, context: StaticContext) -> bool:
    """Whether a family compiler is registered for *name* for this compilation.

    The compilation's own snapshot is consulted first — a per-compilation
    registration overrides a built-in family — and the process-global built-in
    registry answers for every name it installed.
    """
    if name in context.instruction_compilers:
        return True
    return name in _COMPILERS


def compile_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile one non-literal instruction with its registered family compiler."""
    compiler = context.instruction_compilers.get(instruction.name)
    if compiler is None:
        compiler = _COMPILERS[instruction.name]
    return compiler(instruction, context, module)
