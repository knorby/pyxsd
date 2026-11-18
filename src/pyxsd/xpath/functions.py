"""Host function ownership and XDM/elementpath value bridging.

Host callables and XDM function items are invocation-local: a
:class:`HostFunctionToken` is created for one evaluation and is never placed in
a parser's class-level symbol table.  Plain Python callables receive convenient
Python values for singleton atomic arguments; :class:`pyxsd.xdm.XdmFunction`
items receive XDM argument sequences and return XDM sequences, as required by
Task 7.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MethodType
from typing import Any, cast

from elementpath.datatypes import AnyAtomicType, UntypedAtomic
from elementpath.xpath_tokens.base import XPathToken
from elementpath.xpath_tokens.functions import XPathFunction

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmItem, XdmSequence, as_xdm

__all__ = [
    "FUNCTION_SCOPE_ATTRIBUTE",
    "ContextFunction",
    "ContextFunctionToken",
    "HostFunctionToken",
    "XdmFunctionTarget",
    "atomic_to_elementpath",
    "call_host_function",
    "clone_function_token",
    "function_arity",
    "sequence_to_elementpath",
]


#: Private dynamic-context attribute marking the body of an inline/higher-order
#: function call.  It is an attribute rather than a variable so a legal user
#: variable or parameter can never shadow or forge it.
FUNCTION_SCOPE_ATTRIBUTE = "_pyxsd_function_scope"

ContextFunctionCallback = Callable[[XdmItem | None, tuple[XdmSequence, ...]], XdmSequence]


@dataclass(frozen=True)
class ContextFunction:
    """One XSLT-defined function with direct and dynamic-call behavior.

    XSLT context components are available to a direct call in the instruction
    that contains the expression.  XSLT 3.0 section 5.3.4 clears those
    components for a dynamic function call, so a function item such as
    ``current#0`` must not retain the caller's current item, current group, or
    regex captures.  ``dynamic`` supplies that absent-context behavior; when it
    is omitted the direct callback is used with an absent context item.
    """

    direct: ContextFunctionCallback
    dynamic: ContextFunctionCallback | None = None

    def __post_init__(self) -> None:
        if not callable(self.direct):
            raise TypeError("a context function requires a direct callback")
        if self.dynamic is not None and not callable(self.dynamic):
            raise TypeError("a context function dynamic callback must be callable")

    def __call__(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        return self.direct(context_item, arguments)

    def invoke(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
        *,
        dynamic: bool = False,
    ) -> XdmSequence:
        """Invoke the direct or dynamic arm with XDM argument sequences."""
        if dynamic:
            callback = self.dynamic or self.direct
            return callback(None, arguments)
        return self.direct(context_item, arguments)


def _local_type_name(type_name: str) -> str:
    return type_name.partition("}")[2] if type_name.startswith("{") else type_name


def atomic_to_elementpath(item: XdmAtomic) -> Any:
    """Convert one XDM atomic to elementpath's atomic representation."""
    if _local_type_name(item.type_name) == "untypedAtomic":
        return UntypedAtomic(item.lexical)
    value = item.value
    return value if isinstance(value, AnyAtomicType) else item.lexical


def sequence_to_elementpath(
    sequence: XdmSequence,
    *,
    item_converter: Callable[[XdmItem], Any],
) -> Any:
    """Convert an XDM sequence to the single-item/list convention elementpath uses."""
    values = [item_converter(item) for item in sequence]
    if not values:
        return []
    if len(values) == 1:
        return values[0]
    return values


def _host_argument(value: Any) -> Any:
    """Expose one host callable argument without losing XDM item identity."""
    sequence = as_xdm(value)
    if not sequence:
        return ()
    if len(sequence) == 1:
        item = sequence[0]
        if isinstance(item, XdmAtomic):
            return item.value
        return item
    return tuple(item.value if isinstance(item, XdmAtomic) else item for item in sequence)


class XdmFunctionTarget:
    """A host callable that consumes and returns XDM sequences directly.

    ``call_host_function`` invokes :meth:`invoke_xdm` with the evaluated
    argument sequences, so a typed extension adapter can validate cardinality
    and item types before an application callback sees a host value.  The
    object stays callable because expression environments accept host
    callables; that path is not the XSLT extension seam.
    """

    def invoke_xdm(
        self,
        arguments: tuple[XdmSequence, ...],
        environment: Any = None,
    ) -> XdmSequence:
        raise NotImplementedError("an XDM function target must implement invoke_xdm()")

    def __call__(self, *arguments: Any) -> XdmSequence:
        return self.invoke_xdm(tuple(as_xdm(argument) for argument in arguments))


def call_host_function(
    target: Any,
    arguments: Iterable[Any],
    environment: Any = None,
) -> XdmSequence:
    """Invoke a host callable or XDM function item with XDM argument sequences."""
    values = tuple(as_xdm(argument) for argument in arguments)
    if isinstance(target, XdmFunctionTarget):
        return target.invoke_xdm(values, environment)
    if isinstance(target, XdmFunction):
        return target.invoke(values, environment)
    if callable(target):
        result = target(*(_host_argument(value) for value in values))
        return as_xdm(result)
    raise TypeError(f"{type(target).__name__} is not a callable host function")


def function_arity(target: Any) -> int | tuple[int, int | None]:
    """Derive an arity for a host callable or XDM function item."""
    if isinstance(target, XdmFunction):
        return target.arity
    if not callable(target):
        raise TypeError(f"{type(target).__name__} is not callable")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return (0, None)
    minimum = 0
    maximum: int | None = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            if parameter.default is inspect.Parameter.empty:
                raise TypeError(
                    f"callable {target!r} has required keyword-only parameter "
                    f"{parameter.name!r}; XPath host functions are invoked positionally"
                )
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            maximum = None
        elif parameter.default is inspect.Parameter.empty:
            minimum += 1
            if maximum is not None:  # pragma: no branch - positional params precede *args
                maximum += 1
        elif maximum is not None:  # pragma: no branch - positional params precede *args
            maximum += 1
    if maximum == minimum:
        return minimum
    return (minimum, maximum)


class HostFunctionToken(XPathFunction):
    """An invocation-local elementpath function item backed by host code.

    The token is deliberately not registered in any parser symbol table.  It is
    created when an environment variable or function is converted into an
    elementpath value for one evaluation, so elementpath's token mutation bug
    (documented by ``pyxsd.xpath.capabilities``) cannot leak between calls.

    An optional ``context_binder`` re-resolves the target from the calling
    elementpath context on every invocation, so a focus-sensitive function item
    saved in a variable observes each call's context item instead of the focus
    bound when the environment was built.
    """

    symbol = "host-function"
    lookup_name = "host-function"
    label = "external function"
    namespace: str | None = None

    def __init__(
        self,
        parser: Any,
        target: Any,
        *,
        item_converter: Callable[[XdmItem], Any],
        name: str | None = None,
        arity: int | tuple[int, int | None] | None = None,
        context_binder: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(parser)
        if not callable(target) and not isinstance(target, XdmFunction):
            raise TypeError("a host function token requires a callable or XdmFunction")
        self._target = target
        self._item_converter = item_converter
        self._context_binder = context_binder
        self._host_name = name
        self.name = name or ""
        self.nargs = function_arity(target) if arity is None else arity

    @property
    def arity(self) -> int:
        nargs = self.nargs
        if isinstance(nargs, int):
            return nargs
        assert nargs is not None
        return nargs[0]

    def _check(self, count: int) -> None:
        nargs = self.nargs
        minimum: int
        maximum: int | None
        if isinstance(nargs, int):
            minimum = maximum = nargs
        else:
            assert nargs is not None
            minimum, maximum = nargs
        if count < minimum or (maximum is not None and count > maximum):
            raise self.error(
                "XPTY0004",
                f"{self._host_name or 'host function'} expects {minimum}..{maximum} "
                f"arguments, received {count}",
            )

    def _invocation_target(self, context: Any) -> Any:
        """Resolve the target for one call, re-bound to the live focus."""
        binder = self._context_binder
        if binder is None:
            return self._target
        return binder(context)

    def __call__(self, *args: Any, context: Any = None) -> Any:
        target = self._invocation_target(context)
        self._check(len(args))
        arguments = tuple(as_xdm(_evaluate_token(value, context)) for value in args)
        result = call_host_function(target, arguments)
        return sequence_to_elementpath(result, item_converter=self._item_converter)

    def evaluate(self, context: Any = None) -> Any:
        target = self._invocation_target(context)
        arguments = tuple(
            as_xdm(self.get_argument(context, index=index)) for index in range(len(self))
        )
        result = call_host_function(target, arguments)
        return sequence_to_elementpath(result, item_converter=self._item_converter)

    def __repr__(self) -> str:
        return f"<HostFunctionToken {self._host_name or self._target!r}>"


def _evaluate_token(value: Any, context: Any) -> Any:
    evaluate = getattr(value, "evaluate", None)
    if callable(evaluate) and not isinstance(value, (str, bytes, bytearray)):
        return evaluate(context)
    return value


def _in_function_scope(context: Any) -> bool:
    return bool(getattr(context, FUNCTION_SCOPE_ATTRIBUTE, False))


class ContextFunctionToken(XPathFunction):
    """A parser-resident token for one XSLT context-dependent function.

    Direct calls use the active XPath context and pass its context item to the
    ``direct`` callback.  Dynamic calls (including ``function-lookup`` results
    and function references) pass an absent context item and use the ``dynamic``
    callback, which makes XSLT's context-clearing rule explicit instead of
    silently reusing the caller's state.

    The class carries the parser bucket's ``holder`` and never a callback
    target.  An override's implementation is a dynamic-context component (XSLT
    3.0 section 5.3.4), so each call reads this evaluation's target from
    ``holder.overrides`` and its bridge from ``holder.bridge``.  One class per
    parser symbol therefore serves every evaluation that reuses the parser, and
    no process-global transformation lookup exists.
    """

    symbol = "context-function"
    lookup_name = "context-function"
    label = "XSLT-defined function"
    namespace: str | None = None

    #: The bucket holder whose ``overrides`` table names this symbol's target
    #: for the evaluation that is running.
    holder: Any = None
    #: The token class this override replaced.  An override target that is a
    #: plain callable keeps that class's dynamic-call behavior, exactly as it
    #: had when the callback was installed per evaluation.
    plain_base: Any = None

    def __init__(self, parser: Any, nargs: int | None = None) -> None:
        super().__init__(parser, nargs)

    def _target(self) -> Any:
        """This evaluation's implementation for ``self.lookup_name``."""
        holder = self.holder
        target = None if holder is None else holder.overrides.get(self.lookup_name)
        if target is None:
            raise RuntimeError(f"the XSLT function {self.lookup_name!r} has no evaluation context")
        return target

    def _bridge(self) -> Any:
        """The value bridge of the evaluation that is running."""
        holder = self.holder
        bridge = None if holder is None else holder.bridge
        if bridge is None:
            raise RuntimeError(f"the XSLT function {self.lookup_name!r} has no evaluation context")
        return bridge

    def _context_item(self, context: Any, reader: Callable[[Any], XdmSequence]) -> XdmItem | None:
        raw = getattr(context, "item", None) if context is not None else None
        if raw is None:
            return None
        sequence = reader(raw)
        return sequence[0] if sequence else None

    def _arguments(
        self, context: Any, reader: Callable[[Any], XdmSequence]
    ) -> tuple[XdmSequence, ...]:
        arguments: list[XdmSequence] = []
        for index in range(len(self)):
            token = self._items[index]
            evaluate = getattr(token, "evaluate", None)
            value = evaluate(context) if callable(evaluate) else token
            arguments.append(reader(value))
        return tuple(arguments)

    def _finish(
        self,
        result: XdmSequence,
        converter: Callable[[XdmItem], Any],
    ) -> Any:
        return sequence_to_elementpath(as_xdm(result), item_converter=converter)

    def evaluate(self, context: Any = None) -> Any:
        target = self._target()
        bridge = self._bridge()
        active = context if context is not None else self.context
        if not isinstance(target, ContextFunction):
            return _plain_override_result(self, target, bridge, active)
        reader = bridge.to_xdm_sequence
        arguments = self._arguments(active, reader)
        if target.dynamic is not None and _in_function_scope(active):
            result = target.dynamic(None, arguments)
        else:
            result = target.direct(self._context_item(active, reader), arguments)
        return self._finish(result, bridge.adapt_item)

    def __call__(self, *args: Any, context: Any = None) -> Any:
        target = self._target()
        if not isinstance(target, ContextFunction):
            plain_base = self.plain_base
            if plain_base is None:  # pragma: no cover - installed with the token class
                raise RuntimeError(
                    f"the XSLT function {self.lookup_name!r} has no evaluation context"
                )
            return plain_base.__call__(self, *args, context=context)
        bridge = self._bridge()
        dynamic = target.dynamic or target.direct
        arguments = tuple(bridge.to_xdm_sequence(_evaluate_token(value, context)) for value in args)
        return self._finish(dynamic(None, arguments), bridge.adapt_item)

    def __repr__(self) -> str:
        return f"<ContextFunctionToken {self.lookup_name}>"


def _plain_override_result(token: Any, target: Any, bridge: Any, active: Any) -> Any:
    """Call a plain-callable override with the token's argument sequences.

    This is the algorithm the override used while its callback was closed over
    one evaluation: the target and bridge now come from the bucket holder, and
    nothing else changes.
    """
    arguments: list[Any] = []
    for index in range(len(token)):
        arguments.append(bridge.to_xdm_sequence(list(token._items[index].select(active))))
    item = None
    raw_item = getattr(active, "item", None) if active is not None else None
    if raw_item is not None:
        item = next(iter(bridge.to_xdm_sequence(raw_item)), None)
    return bridge.sequence_to_elementpath(as_xdm(target(item, tuple(arguments))))


def clone_function_token(token: Any) -> Any:
    """Return an isolated callable token for one deferred function invocation.

    elementpath's ``XPathFunction.__call__`` clears and repopulates the token's
    child list.  A shallow token copy plus detached mutable containers keeps
    later invocations from sharing that state without deep-copying the parser
    or an invocation context that may contain immutable mapping proxies.
    """
    if not callable(token):
        raise TypeError("a copied XPath function item is not callable")
    return cast(Any, _clone_token_tree(token))


def _clone_token_tree(value: Any) -> Any:
    """Copy token state recursively without copying a parser or context."""
    if not isinstance(value, XPathToken):
        return value
    clone = copy.copy(value)
    items = getattr(value, "_items", None)
    if isinstance(items, list):
        clone._items = [_clone_token_tree(item) for item in items]
    values = getattr(value, "_values", None)
    if isinstance(values, list):
        cast(Any, clone)._values = [_clone_token_tree(item) for item in values]
    for attribute in ("sequence_types", "varnames"):
        attribute_value = getattr(value, attribute, None)
        if isinstance(attribute_value, list):
            setattr(clone, attribute, list(attribute_value))
    body = getattr(value, "body", None)
    if isinstance(body, XPathToken):
        cast(Any, clone).body = _clone_token_tree(body)
    variables = getattr(value, "variables", None)
    if isinstance(variables, dict):
        cast(Any, clone).variables = dict(variables)
    for attribute in ("_partial_evaluate", "_partial_select"):
        method = getattr(value, attribute, None)
        function = getattr(method, "__func__", None)
        if getattr(method, "__self__", None) is value and function is not None:
            setattr(clone, attribute, MethodType(function, clone))
    return clone
