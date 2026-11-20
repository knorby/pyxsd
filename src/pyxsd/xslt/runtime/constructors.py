"""Execution of the mandatory constructed-node instruction family.

Each executor evaluates one constructor into a detached comment, processing
instruction, or namespace node.  The scheduler's existing complex-content rules
attach a returned node, so nothing here knows about the open element: namespace
nodes reach :meth:`_NodeFactory.attach_namespace` through the generic attachment
path, which is what enforces position and duplicate-binding errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from elementpath.datatypes import AnyURI
from elementpath.helpers import is_ncname

from pyxsd.xdm import XdmSequence
from pyxsd.xslt.compiler.plans import (
    _CommentPlan,
    _NamespacePlan,
    _Plan,
    _ProcessingInstructionPlan,
)
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.builder import XML_NS, XMLNS_NS, _NodeFactory
from pyxsd.xslt.runtime.dispatch import PlanExecutor
from pyxsd.xslt.runtime.instructions import _simple_content_string

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.processor import _Invocation
    from pyxsd.xslt.runtime.context import EvaluationContext

__all__ = [
    "CONSTRUCTOR_PLAN_EXECUTORS",
    "execute_comment",
    "execute_namespace",
    "execute_processing_instruction",
]


def _value(
    plan: _CommentPlan | _ProcessingInstructionPlan | _NamespacePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> str:
    """Evaluate a constructor's value with the simple-content string rules."""
    if plan.expression is not None:
        sequence = invocation._evaluate(plan.expression, context)
    else:
        sequence = invocation._execute_plans(plan.children, context=context)
    return _simple_content_string(sequence, " ")


def execute_comment(
    plan: _CommentPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Construct one comment node."""
    return (_NodeFactory().comment(_value(plan, invocation, context)),)


def execute_processing_instruction(
    plan: _ProcessingInstructionPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Construct one processing-instruction node with a validated target."""
    target = invocation._value_source(plan.name, context)
    if not is_ncname(target) or target.lower() == "xml":
        raise XsltDynamicError(
            f"the processing-instruction target {target!r} is not a valid PITarget",
            code="XTDE0890",
        )
    return (_NodeFactory().processing_instruction(target, _value(plan, invocation, context)),)


def _any_uri(value: str, xsd_version: str) -> bool:
    """Whether *value* belongs to the profile's ``xs:anyURI`` lexical space.

    XSD 1.1 deliberately widened ``xs:anyURI`` to admit every string; only a
    1.0 profile keeps the URI-reference syntax check.
    """
    if xsd_version != "1.0":
        return True
    try:
        AnyURI(value)
    except ValueError:
        return False
    return True


def execute_namespace(
    plan: _NamespacePlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    """Construct one detached namespace node for later attachment."""
    prefix = invocation._value_source(plan.name, context)
    if prefix == "xmlns" or (prefix != "" and not is_ncname(prefix)):
        raise XsltDynamicError(
            f"the namespace prefix {prefix!r} is not a valid namespace name",
            code="XTDE0920",
        )
    uri = _value(plan, invocation, context)
    if uri == "":
        raise XsltDynamicError("a namespace node requires a non-empty URI", code="XTDE0930")
    xsd_version = invocation.stylesheet.processor.xsd_version
    if uri == XMLNS_NS or not _any_uri(uri, xsd_version):
        raise XsltDynamicError(
            f"the namespace URI {uri!r} is not a permitted anyURI",
            code="XTDE0905",
        )
    if (prefix == "xml" and uri != XML_NS) or (uri == XML_NS and prefix != "xml"):
        raise XsltDynamicError(
            "the xml prefix and XML namespace must be used together",
            code="XTDE0925",
        )
    return (_NodeFactory().namespace(prefix, uri, parent=None),)


#: The constructed-node family's per-artifact executor registration.
CONSTRUCTOR_PLAN_EXECUTORS: Mapping[type[_Plan], PlanExecutor] = MappingProxyType(
    {
        _CommentPlan: execute_comment,
        _ProcessingInstructionPlan: execute_processing_instruction,
        _NamespacePlan: execute_namespace,
    }
)
