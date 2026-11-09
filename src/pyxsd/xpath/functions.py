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
from types import MethodType
from typing import Any, cast

from elementpath.datatypes import AnyAtomicType, UntypedAtomic
from elementpath.xpath_tokens.base import XPathToken
from elementpath.xpath_tokens.functions import XPathFunction

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmItem, XdmSequence, as_xdm

__all__ = [
    "HostFunctionToken",
    "atomic_to_elementpath",
    "call_host_function",
    "clone_function_token",
    "function_arity",
    "sequence_to_elementpath",
]


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


def call_host_function(
    target: Any,
    arguments: Iterable[Any],
    environment: Any = None,
) -> XdmSequence:
    """Invoke a host callable or XDM function item with XDM argument sequences."""
    values = tuple(as_xdm(argument) for argument in arguments)
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
    ) -> None:
        super().__init__(parser)
        if not callable(target) and not isinstance(target, XdmFunction):
            raise TypeError("a host function token requires a callable or XdmFunction")
        self._target = target
        self._item_converter = item_converter
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

    def __call__(self, *args: Any, context: Any = None) -> Any:
        self._check(len(args))
        arguments = tuple(as_xdm(_evaluate_token(value, context)) for value in args)
        result = call_host_function(self._target, arguments)
        return sequence_to_elementpath(result, item_converter=self._item_converter)

    def evaluate(self, context: Any = None) -> Any:
        arguments = tuple(
            as_xdm(self.get_argument(context, index=index)) for index in range(len(self))
        )
        result = call_host_function(self._target, arguments)
        return sequence_to_elementpath(result, item_converter=self._item_converter)

    def __repr__(self) -> str:
        return f"<HostFunctionToken {self._host_name or self._target!r}>"


def _evaluate_token(value: Any, context: Any) -> Any:
    evaluate = getattr(value, "evaluate", None)
    if callable(evaluate) and not isinstance(value, (str, bytes, bytearray)):
        return evaluate(context)
    return value


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
