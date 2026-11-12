"""Name-keyed dispatch of normalized XSLT instructions to their compilers.

Each instruction family owns one dedicated compiler.  The registry is keyed by
the instruction's expanded XSLT name, so a normalized node selects its family
without a central conditional.  Literal result elements and unregistered
instructions stay outside the registry: literal construction is a distinct plan
shape, and forwards-compatible or unsupported instructions keep their explicit
handling in the instruction compiler.

Registration is declarative.  The compilation module installs every family
compiler in one mapping when it is imported; dispatch itself only performs the
expanded-name lookup and call.
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


def supports_instruction(name: str) -> bool:
    """Whether a dedicated family compiler is registered for *name*."""
    return name in _COMPILERS


def compile_instruction(
    instruction: Instruction,
    context: StaticContext,
    module: EffectiveModule,
) -> _Plan:
    """Compile one non-literal instruction with its registered family compiler."""
    return _COMPILERS[instruction.name](instruction, context, module)
