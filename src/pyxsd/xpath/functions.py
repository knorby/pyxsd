"""Host function ownership and XDM/elementpath value bridging.

Host callables and XDM function items are invocation-local: a
:class:`HostFunctionToken` is created for one evaluation and is never placed in
a parser's class-level symbol table.  Plain Python callables receive convenient
Python values for singleton atomic arguments; :class:`pyxsd.xdm.XdmFunction`
items receive XDM argument sequences and return XDM sequences, as required by
Task 7.

:func:`install_string_function` corrects the substrate's ``fn:string``
conversion for floats and doubles, which elementpath spells with the shortest
binary64 ``repr`` instead of the F&O 3.1 §19.1.1 canonical form.

:func:`format_datetime_value` and :func:`format_integer_value` are the reusable
formatting components behind ``fn:format-date``, ``fn:format-time``,
``fn:format-dateTime``, and ``fn:format-integer``.  A native XSLT 2.0 processor
must supply those F&O 2.0 functions itself because its XPath 2.0 parser has no
symbol for them.  The component reuses the engine's date/time substrate for the
picture grammar and marker evaluation and supplies the rules that substrate
does not: escaped picture brackets, width padding for roman numerals, the
XSLT 2.0 timezone "omit zero minutes" form, ``am``/``pm`` words, and the
implementation-defined locale fallbacks that the corresponding W3C tests
expect.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from types import MethodType
from typing import Any, cast

from elementpath.datatypes import AnyAtomicType, Date, DateTime, QName, Time, UntypedAtomic
from elementpath.exceptions import xpath_error
from elementpath.xpath_nodes import XPathNode
from elementpath.xpath_tokens.base import XPathToken
from elementpath.xpath_tokens.functions import XPathFunction

from pyxsd import xsd_data_types as _xsd
from pyxsd.namespaces import XSD_NS
from pyxsd.xdm import XdmAtomic, XdmFunction, XdmItem, XdmSequence, as_xdm
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xdm.values import canonical_decimal_string, canonical_float_string
from pyxsd.xpath import continuation
from pyxsd.xpath.continuation import XPathClosure

__all__ = [
    "FUNCTION_SCOPE_ATTRIBUTE",
    "ContextFunction",
    "ContextFunctionToken",
    "HostFunctionToken",
    "XdmFunctionTarget",
    "atomic_to_elementpath",
    "call_host_function",
    "canonical_string_value",
    "clone_function_token",
    "format_datetime_value",
    "format_integer_value",
    "function_arity",
    "install_date_time",
    "install_deep_equal",
    "install_regex_replace",
    "install_string_conversion_functions",
    "install_string_function",
    "sequence_to_elementpath",
    "xdm_deep_equal",
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
    """Convert one XDM atomic to elementpath's atomic representation.

    Python ``bool`` is elementpath's ``xs:boolean``.  A pyxsd
    ``xsd_data_types.Boolean`` is an ``int`` subclass over the XSD value space,
    so passing that payload through unchanged made every bridged boolean test
    as ``xs:integer`` instead of ``xs:boolean``.

    The same identity problem applies to every pyxsd datatype that elementpath
    models with a dedicated class: ``xs:float``, the string-derived types,
    ``xs:anyURI``, the date/time and duration families, the binary types, and
    ``xs:QName``.  Those payloads are converted to elementpath's class so a
    typed value keeps its XSD type identity through ``instance of``,
    ``treat as``, and the typed function signatures.
    """
    local = _local_type_name(item.type_name)
    if local == "untypedAtomic":
        return UntypedAtomic(item.lexical)
    value = item.value
    if not _is_builtin_type_name(item.type_name):
        # A schema-defined type has no elementpath class of its own; the value
        # is bridged through its primitive root so operators, casts, and
        # comparisons keep working.  The user type name stays in the XdmAtomic
        # and the schema proxy consults the annotation recorded beside the
        # converted object.
        root = _root_builtin_local(value)
        if root is not None:
            local = root
    if local == "boolean":
        return bool(item.value)
    if local in _EP_ROOT_BUILTIN_LOCALS or isinstance(value, AnyAtomicType):
        return _elementpath_datatype_value(value, local)
    return item.lexical


def _root_builtin_local(value: Any) -> str | None:
    """The primitive built-in local name of one pyxsd datatype payload."""
    from elementpath.datatypes import builtin_atomic_types

    for base in type(value).__mro__:
        if base.__module__ != _xsd.__name__:
            # A generated or test datatype may carry a bookkeeping ``name``
            # that deliberately disagrees with its primitive payload.
            continue
        name = base.__dict__.get("name")
        if isinstance(name, str) and f"xs:{name}" in builtin_atomic_types:
            return name
    return None


def _is_builtin_type_name(type_name: str) -> bool:
    """Whether *type_name* names a built-in XSD atomic type.

    A user-defined type can reuse a built-in local name, so only the XSD
    namespace may select elementpath's class for a bridged value.
    """
    if type_name.startswith("{"):
        return type_name.startswith(f"{{{XSD_NS}}}")
    return ":" not in type_name


#: Elementpath classes whose instances build from their XSD lexical string.
_EP_LEXICAL_DATATYPES = frozenset(
    {
        "anyURI",
        "base64Binary",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "hexBinary",
        "ID",
        "IDREF",
        "ENTITY",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "normalizedString",
        "token",
    }
)

#: Elementpath classes whose payload can be built from a host value directly.
_EP_ROOT_BUILTIN_LOCALS = frozenset(
    {
        "boolean",
        "decimal",
        "double",
        "float",
        "integer",
        "string",
    }
    | {
        "byte",
        "int",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
    | {
        "anyURI",
        "base64Binary",
        "date",
        "dateTime",
        "dateTimeStamp",
        "dayTimeDuration",
        "duration",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "hexBinary",
        "NOTATION",
        "QName",
        "time",
        "yearMonthDuration",
    }
)


#: Elementpath integer-derived classes: the payload is the Python integer.
_EP_INTEGER_DATATYPES = frozenset(
    {
        "byte",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)

#: Elementpath date/time and duration classes built through ``fromstring``.
_EP_FROMSTRING_DATATYPES = frozenset(
    {
        "date",
        "dateTime",
        "dateTimeStamp",
        "dayTimeDuration",
        "duration",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "time",
        "yearMonthDuration",
    }
)


#: Elementpath QName-family classes built from the resolved name parts.
_EP_QNAME_DATATYPES = frozenset({"NOTATION", "QName"})


def _elementpath_datatype_value(value: Any, local: str) -> Any:
    """Elementpath's own class for one pyxsd datatype value, when it has one."""
    from elementpath.datatypes import builtin_atomic_types

    datatype = builtin_atomic_types.get(f"xs:{local}")
    if datatype is None or isinstance(value, datatype):
        return value
    if local in _EP_QNAME_DATATYPES:
        if not getattr(value, "_resolved_", False):
            return value
        prefix = getattr(value, "_prefix_", "")
        name = getattr(value, "_local_", "")
        lexical = f"{prefix}:{name}" if prefix else name
        try:
            return datatype(getattr(value, "_uri_", None) or "", lexical)
        except (TypeError, ValueError):  # pragma: no cover - the source QName is valid
            return value
    if local == "float":
        try:
            return datatype(float(value))
        except (TypeError, ValueError):  # pragma: no cover - the source value is a binary32
            return value
    if local in _EP_INTEGER_DATATYPES:
        try:
            return datatype(int(value))
        except (TypeError, ValueError):  # pragma: no cover - the source is a bounded integer
            return value
    if local in _EP_FROMSTRING_DATATYPES and hasattr(datatype, "fromstring"):
        try:
            return datatype.fromstring(str(value))
        except (TypeError, ValueError):  # pragma: no cover - the source already parsed
            return value
    if local in _EP_LEXICAL_DATATYPES:
        try:
            return datatype(str(value))
        except (TypeError, ValueError):  # pragma: no cover - the source already parsed
            return value
    return value  # pragma: no cover - every built-in XSD atomic type has a rule above


def canonical_string_value(obj: Any) -> str:
    """The F&O canonical conversion for one atomic value.

    ``fn:string`` and every construct that spells an atomic value as text (the
    ``concat`` arguments, the items of ``string-join``, and XSLT text
    construction) share this conversion.  Floats and doubles use the shortest
    binary form with F&O's scientific spelling, and a decimal drops trailing
    fractional zeros.
    """
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, float):
        single = getattr(type(obj), "name", None) == "float"
        return canonical_float_string(float(obj), single=single)
    if isinstance(obj, Decimal):
        return canonical_decimal_string(Decimal(obj))
    if isinstance(obj, UntypedAtomic):
        return obj.value
    return str(obj)


def install_string_function(parser: Any) -> None:
    """Give ``fn:string``/``xs:string`` the F&O canonical float/double form.

    elementpath converts a float or double by ``repr``: it writes ``1e6`` as
    ``1000000`` and ``1e-13`` as ``1E-13``, while F&O 3.1 §19.1.1 requires
    ``1.0E6`` and ``1.0E-13`` (one digit before the point, at least one after),
    and uses the shortest binary32 form for ``xs:float``.  Only the parser
    instance's ``string`` token changes; every other conversion keeps the
    substrate's own behavior.
    """
    base = parser.symbol_table["string"]

    def string_value(self: Any, obj: Any) -> str:
        if isinstance(obj, (bool, float, Decimal, UntypedAtomic)):
            return canonical_string_value(obj)
        return base.string_value(self, obj)

    parser._register(
        "string",
        "_PyxsdStringFunction",
        bases=(base,),
        label=base.label,
        nargs=getattr(base, "nargs", (0, 1)),
        sequence_types=getattr(base, "sequence_types", ()),
        string_value=string_value,
    )


def install_string_conversion_functions(parser: Any) -> None:
    """Give ``concat`` and ``string-join`` the canonical atomic conversion.

    Both functions spell every argument/item as a string, so they must use the
    same conversion as ``fn:string``.  The substrate calls its own
    ``string_value``, which keeps the ``repr`` spelling for a double and a
    decimal's trailing zeros, so their result disagreed with ``fn:string``.
    """
    concat_base = parser.symbol_table["concat"]

    def evaluate_concat(self: Any, context: Any = None) -> str:
        texts: list[str] = []
        for index in range(len(self)):
            value = self.get_argument(context, index=index)
            if value is None:  # pragma: no cover - optional concat arguments
                continue
            if isinstance(value, XPathNode):
                texts.append(self.string_value(value))
            else:
                texts.append(canonical_string_value(value))
        return "".join(texts)

    parser._register(
        "concat",
        "_PyxsdConcatFunction",
        bases=(concat_base,),
        label=concat_base.label,
        nargs=getattr(concat_base, "nargs", (2, None)),
        sequence_types=getattr(concat_base, "sequence_types", ()),
        evaluate=evaluate_concat,
    )

    join_base = parser.symbol_table["string-join"]

    def evaluate_string_join(self: Any, context: Any = None) -> str:
        separator = self.get_argument(context, index=1, required=True, cls=str)
        return separator.join(
            canonical_string_value(item)
            for item in self[0].atomization(context)
            if item is not None
        )

    parser._register(
        "string-join",
        "_PyxsdStringJoinFunction",
        bases=(join_base,),
        label=join_base.label,
        nargs=getattr(join_base, "nargs", 2),
        sequence_types=getattr(join_base, "sequence_types", ()),
        evaluate=evaluate_string_join,
    )


def _xdm_node(value: Any) -> Any:
    """The grounded XDM node behind one value, when it has one."""
    if isinstance(value, XdmNode):
        return value
    if isinstance(value, XPathNode):
        return getattr(value, "xdm", None)
    return None


def xdm_deep_equal(left: Iterable[Any], right: Iterable[Any]) -> bool:
    """``fn:deep-equal`` over elementpath values with grounded XDM nodes.

    elementpath compares a generic ``XPathNode`` by its ``value`` attribute;
    pyxsd's grounded adapters keep an XDM node there, so two structurally equal
    elements were never deep-equal.  Nodes are compared structurally here and
    every other item keeps elementpath's own F&O rules (NaN equality, atomic
    conversion, map and array comparison).  Namespace nodes do not participate
    in element comparison, as XDM requires.
    """
    from elementpath.compare import deep_equal as _deep_equal

    left_values = tuple(left)
    right_values = tuple(right)
    if len(left_values) != len(right_values):
        return False
    for first, second in zip(left_values, right_values, strict=True):
        first_node = _xdm_node(first)
        second_node = _xdm_node(second)
        if first_node is not None or second_node is not None:
            if first_node is None or second_node is None:
                return False
            if not _xdm_nodes_deep_equal(first_node, second_node):
                return False
            continue
        if not _deep_equal((first,), (second,)):
            return False
    return True


def _xdm_nodes_deep_equal(left: Any, right: Any) -> bool:
    """Structural deep equality for two grounded XDM nodes."""
    from pyxsd.xdm.nodes import NodeKind

    if left.kind is not right.kind:
        return False
    if left.kind is NodeKind.ELEMENT:
        if left.name != right.name:
            return False
        first_attributes = {attribute.name: attribute.string_value for attribute in left.attributes}
        second_attributes = {
            attribute.name: attribute.string_value for attribute in right.attributes
        }
        if first_attributes != second_attributes:
            return False
        return _xdm_children_deep_equal(left, right)
    if left.kind is NodeKind.ATTRIBUTE:
        return left.name == right.name and left.string_value == right.string_value
    if left.kind in (NodeKind.TEXT, NodeKind.COMMENT):
        return left.string_value == right.string_value
    if left.kind is NodeKind.PROCESSING_INSTRUCTION:
        return left.name == right.name and left.string_value == right.string_value
    if left.kind is NodeKind.NAMESPACE:
        return left.prefix == right.prefix and left.string_value == right.string_value
    return _xdm_children_deep_equal(left, right)


def _xdm_children_deep_equal(left: Any, right: Any) -> bool:
    if len(left.children) != len(right.children):
        return False
    return all(
        _xdm_nodes_deep_equal(first, second)
        for first, second in zip(left.children, right.children, strict=True)
    )


def install_deep_equal(parser: Any) -> None:
    """Install the XDM-aware two-argument ``fn:deep-equal``."""
    base = parser.symbol_table["deep-equal"]

    def evaluate_deep_equal(self: Any, context: Any = None) -> Any:
        if len(self) != 2:
            return base.evaluate(self, context)
        return xdm_deep_equal(
            self[0].select(context),
            self[1].select(context),
        )

    parser._register(
        "deep-equal",
        "_PyxsdDeepEqualFunction",
        bases=(base,),
        label=base.label,
        nargs=getattr(base, "nargs", 2),
        sequence_types=getattr(base, "sequence_types", ()),
        evaluate=evaluate_deep_equal,
    )


def install_date_time(parser: Any) -> None:
    """Give ``fn:dateTime`` the XPath function-conversion rules.

    elementpath's token reads both arguments with ``get_argument(cls=Date)``,
    which rejects an untyped node argument instead of atomizing it and casting
    an untyped atomic value to ``xs:date``/``xs:time``.  The two-argument form
    converts both operands here and then delegates to the same normative
    combine logic.
    """
    from elementpath.datatypes import Date, Time
    from elementpath.xpath_tokens.tokens import ValueToken

    base = parser.symbol_table["dateTime"]

    def evaluate_date_time(self: Any, context: Any = None) -> Any:
        if len(self) != 2:
            return base.evaluate(self, context)
        values: list[Any] = []
        for index, cls in ((0, Date), (1, Time)):
            items = tuple(self[index].select(context))
            if len(items) > 1 and not self.parser.compatibility_mode:
                raise self.error("XPTY0004", "a dateTime argument must be a single value")
            values.append(None if not items else _date_time_argument(self, items[0], cls))
        saved = list(self._items)
        try:
            # ValueToken keeps ``None`` as the class sentinel, so an empty
            # operand must be represented by an empty list; otherwise select()
            # yields the literal string ``'(value)'`` and the base token reports
            # a spurious type error instead of returning the empty sequence.
            self[0:] = tuple(
                ValueToken(self.parser, value=[] if value is None else value) for value in values
            )
            return base.evaluate(self, context)
        finally:
            self[0:] = tuple(saved)

    parser._register(
        "dateTime",
        "_PyxsdDateTimeFunction",
        bases=(base,),
        label=base.label,
        nargs=getattr(base, "nargs", (1, 2)),
        sequence_types=getattr(base, "sequence_types", ()),
        evaluate=evaluate_date_time,
    )


def _date_time_argument(token: Any, argument: Any, cls: type[Any]) -> Any:
    """Apply function conversion to one ``fn:dateTime`` operand."""
    if isinstance(argument, cls):
        return argument
    node = getattr(argument, "xdm", None)
    if node is not None:
        typed = node.typed_value
        if len(typed) != 1:
            raise token.error("XPTY0004", "a dateTime argument must be a single value")
        argument = typed[0]
    if isinstance(argument, XdmAtomic):
        if isinstance(argument.value, cls):
            return argument.value
        if _local_atomic_name(argument.type_name) != "untypedAtomic":
            raise token.error("XPTY0004", "a dateTime argument has the wrong type")
        lexical = argument.lexical
    elif isinstance(argument, UntypedAtomic):
        lexical = str(argument)
    else:
        raise token.error("XPTY0004", "a dateTime argument has the wrong type")
    try:
        return cls.fromstring(lexical)
    except (TypeError, ValueError) as exc:
        raise token.error("FORG0001", str(exc)) from None


def _local_atomic_name(type_name: str) -> str:
    return type_name.rpartition("}")[2].rpartition(":")[2]


def install_regex_replace(parser: Any) -> None:
    """Give ``fn:replace`` the F&O replacement-string grammar.

    The substrate translates a group reference only when it is not preceded by
    a backslash, so ``\\\\$0`` (an escaped backslash followed by group zero)
    became a literal ``$0``.  It also leaves a reference to a group the pattern
    does not define in the result, where F&O substitutes the empty string.
    This parses the replacement once into literal text and group references and
    expands each match from that form.  A ``q`` (literal pattern) call keeps the
    substrate's replacement handling, because that path deliberately protects
    every backslash from the regular-expression engine.
    """
    import re

    from elementpath.regex import RegexError, translate_pattern

    base = parser.symbol_table.get("replace")
    if base is None:  # pragma: no cover - every supported parser declares it
        return

    def replace_function(self: Any, context: Any = None) -> str:
        input_string: str = self.get_argument(context, default="", cls=str)
        pattern: str = self.get_argument(context, 1, required=True, cls=str)
        replacement: str = self.get_argument(context, 2, required=True, cls=str)
        flags = 0
        literal_pattern = False
        if len(self) > 3:
            for character in self.get_argument(context, 3, required=True, cls=str):
                if character in "smix":
                    flags |= getattr(re, character.upper())
                elif character == "q" and self.parser.version not in ("1.0", "2.0"):
                    literal_pattern = True
                else:
                    raise self.error("FORX0001", f"Invalid regular expression flag {character!r}")
        try:
            translated = (
                re.escape(pattern)
                if literal_pattern
                else translate_pattern(pattern, flags, self.parser.xsd_version)
            )
            compiled = re.compile(translated, flags=flags)
        except (re.error, RegexError, OverflowError) as err:
            raise self.error("FORX0002", f"Invalid regular expression: {err}") from None
        if compiled.search(""):
            raise self.error(
                "FORX0003", f"Regular expression {pattern!r} matches zero-length string"
            )
        if literal_pattern:
            # ``q`` makes the whole pattern literal.  The replacement is taken
            # literally too, so no backslash or ``$`` is an escape here.
            return compiled.sub(lambda match: replacement, input_string)
        holder = getattr(self.parser, "_pyxsd_bridge_holder", None)
        bridge = getattr(holder, "bridge", None) if holder is not None else None
        static = getattr(bridge, "static_context", None)
        xslt_version = getattr(static, "xslt_version", None)
        if xslt_version is None:
            strict = self.parser.version in ("1.0", "2.0")
        else:
            strict = xslt_version in ("1.0", "2.0")
        parts = _replacement_parts(replacement, self, compiled.groups, strict=strict)
        return compiled.sub(lambda match: _expand_replacement(match, parts), input_string)

    parser._register(
        "replace",
        "_PyxsdReplaceFunction",
        bases=(base,),
        label=base.label,
        nargs=getattr(base, "nargs", (3, 4)),
        sequence_types=getattr(base, "sequence_types", ()),
        evaluate=replace_function,
    )


def _replacement_parts(
    replacement: str,
    token: Any,
    groups: int,
    *,
    strict: bool,
) -> list[str | int]:
    """Split one F&O replacement string into literal text and group numbers.

    A reference is the longest digit run that names a group, so ``$25`` with
    eleven groups is group 2 followed by the literal ``5``; a run whose first
    digit already exceeds the group count is the empty string.  F&O 2.0 rejects
    a backslash before any character other than ``\\`` or ``$`` (FORX0004); the
    3.0 replacement grammar keeps both characters.
    """
    parts: list[str | int] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            parts.append("".join(literal))
            literal.clear()

    index = 0
    while index < len(replacement):
        character = replacement[index]
        if character == "\\":
            if index + 1 >= len(replacement):
                raise token.error("FORX0004", f"Invalid replacement string {replacement!r}")
            escaped = replacement[index + 1]
            if escaped in ("\\", "$"):
                literal.append(escaped)
            elif strict:
                raise token.error("FORX0004", f"Invalid replacement string {replacement!r}")
            else:
                literal.extend(("\\", escaped))
            index += 2
            continue
        if character == "$":
            end = index + 1
            while end < len(replacement) and replacement[end].isdigit():
                end += 1
            if end == index + 1:
                raise token.error("FORX0004", f"Invalid replacement string {replacement!r}")
            digits = replacement[index + 1 : end]
            width = len(digits)
            while width and int(digits[:width]) > groups:
                width -= 1
            flush()
            if width == 0:
                parts.append("")
            else:
                parts.append(int(digits[:width]))
                literal.append(digits[width:])
            index = end
            continue
        literal.append(character)
        index += 1
    flush()
    return parts


def _expand_replacement(match: Any, parts: list[str | int]) -> str:
    text: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text.append(part)
        elif part == 0:
            text.append(match.group(0))
        elif part <= match.re.groups:  # pragma: no branch - parser clamps parts
            text.append(match.group(part) or "")
    return "".join(text)


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


def _host_function_qname(name: str | None) -> QName | None:
    """The QName a host function item reports to ``fn:function-name``."""
    if not name:
        return None
    if name.startswith("{"):
        uri, _, local = name[1:].partition("}")
        return QName(uri, local) if local else None
    return QName(None, name)


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
        to_xdm: Callable[[Any], XdmSequence] | None = None,
    ) -> None:
        super().__init__(parser)
        if not callable(target) and not isinstance(target, XdmFunction):
            raise TypeError("a host function token requires a callable or XdmFunction")
        self._target = target
        self._item_converter = item_converter
        self._context_binder = context_binder
        self._to_xdm = to_xdm
        self._host_name = name
        self.name = name or ""
        self._qname = _host_function_qname(name)
        self.nargs = function_arity(target) if arity is None else arity

    @property
    def qname(self) -> QName | None:
        """The function item's name, or ``None`` for an unnamed host callable."""
        return self._qname

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

    def _arguments(self, values: Iterable[Any], context: Any) -> tuple[XdmSequence, ...]:
        """Convert elementpath argument values back to XDM sequences.

        An argument may be a grounded node adapter, a list of adapters, or an
        atomic value.  ``as_xdm`` only accepts XDM items, so a node argument
        needs the evaluation's bridge; without it a function item called with
        ``@name`` raised instead of applying the implicit atomization every
        function call performs.
        """
        convert = self._to_xdm
        if convert is None:
            return tuple(as_xdm(value) for value in values)
        return tuple(convert(value) for value in values)

    def _invoke(self, target: Any, arguments: tuple[XdmSequence, ...]) -> XdmSequence:
        """Invoke one host target, stepping a captured inline closure directly."""
        closure = target.closure if isinstance(target, XdmFunction) else None
        if isinstance(closure, XPathClosure):
            native = tuple(
                sequence_to_elementpath(argument, item_converter=self._item_converter)
                for argument in arguments
            )
            result = continuation.evaluate_closure(closure, native)
            convert = self._to_xdm
            return as_xdm(result) if convert is None else convert(result)
        return call_host_function(target, arguments)

    def __call__(self, *args: Any, context: Any = None) -> Any:
        target = self._invocation_target(context)
        self._check(len(args))
        arguments = self._arguments((_evaluate_token(value, context) for value in args), context)
        result = self._invoke(target, arguments)
        return sequence_to_elementpath(result, item_converter=self._item_converter)

    def evaluate(self, context: Any = None) -> Any:
        target = self._invocation_target(context)
        arguments = self._arguments(
            (self.get_argument(context, index=index) for index in range(len(self))),
            context,
        )
        result = self._invoke(target, arguments)
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


# ---------------------------------------------------------------------------
# F&O formatting components
#
# A native XSLT 2.0 processor has no ``fn:format-date`` and friends in its
# XPath 2.0 parser, so the XSLT layer supplies them.  The substrate already
# owns the XPath 3.0 picture grammar and the marker value computation; these
# helpers add the rules its implementation does not provide.  Calendar and
# language support are deliberately implementation-defined: the fallback is
# English and the proleptic Gregorian (AD) calendar, and every fallback is
# declared in the result exactly as F&O requires.


#: Components a date value has no data for.  ``format-date`` raises FOFD1350.
_DATE_INVALID_COMPONENTS = frozenset("HhPmsf")

#: Components a time value has no data for.  ``format-time`` raises FOFD1350.
_TIME_INVALID_COMPONENTS = frozenset("YMDdFWwCE")

#: Calendars this implementation understands.  ``ISO`` and ``OS`` are the
#: XSLT 2.0 spellings of the proleptic Gregorian calendar.
_IMPLEMENTED_CALENDARS = frozenset({"AD", "ISO", "OS"})

#: Languages the substrate has name tables for.  Every other language uses
#: the implementation-defined English fallback.
_IMPLEMENTED_LANGUAGES = frozenset({"en", "it"})

_DATETIME_CLASSES: dict[str, type[Any]] = {
    "date": Date,
    "time": Time,
    "dateTime": DateTime,
}


def _picture_parts(picture: str) -> tuple[list[str], list[str]]:
    """Split a picture into literals and markers, honouring ``[[``/``]]``.

    A ``[[`` or ``]]`` pair is a literal bracket, so a marker after or before
    one is still recognised: ``[[[Y0001]]]`` is ``[`` + ``[Y0001]`` + ``]``.
    The substrate's splitter treats the ``[`` of ``[[`` as the start of a
    literal ``[QQQ]`` marker, which makes an escaped marker picture an error.
    """
    literals: list[str] = []
    markers: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(picture):
        character = picture[index]
        if character == "[":
            if index + 1 < len(picture) and picture[index + 1] == "[":
                buffer.append("[")
                index += 2
                continue
            close = picture.find("]", index + 1)
            if close < 0:
                raise xpath_error("FOFD1340", "Invalid character '[' in picture literal")
            marker = picture[index + 1 : close]
            if "[" in marker:
                raise xpath_error("FOFD1340", "Invalid character '[' in picture literal")
            literals.append("".join(buffer))
            buffer = []
            markers.append(marker.replace(" ", "").replace("\n", "").replace("\t", ""))
            index = close + 1
            continue
        if character == "]":
            if index + 1 < len(picture) and picture[index + 1] == "]":
                buffer.append("]")
                index += 2
                continue
            raise xpath_error("FOFD1340", "Invalid character ']' in picture literal")
        buffer.append(character)
        index += 1
    literals.append("".join(buffer))
    return literals, markers


def _marker_parts(marker: str) -> tuple[str, str, str]:
    """Return the component, presentation, and width of one marker."""
    component = marker[0]
    token = marker[1:]
    presentation, separator, width = token.rpartition(",")
    if not separator:
        return component, token, ""
    return component, presentation, width


def _parse_width(width: str) -> tuple[int, int | None]:
    from elementpath.xpath30.xpath30_helpers import parse_width

    return parse_width(width)


def _with_presentation_width(
    text: str,
    component: str,
    presentation: str,
    width: str,
) -> str:
    """Pad one textual presentation to its width modifier.

    The substrate applies the width only to the numeric spelling; a roman
    numeral is returned as-is.  F&O applies the minimum width to every
    presentation: the year is padded on the right, every other component on
    the left.  A maximum does not truncate a roman numeral: the W3C
    ``format-date`` width cases require ``dcccxvii`` to survive ``[Yi,4-4]``.
    """
    del presentation
    if not width:
        return text
    minimum, _maximum = _parse_width(width)
    if len(text) < minimum:
        padding = " " * (minimum - len(text))
        text = text + padding if component == "Y" else padding + text
    return text


def _fractional_digits(presentation: str, width: str) -> int:
    """How many fractional-second digits a picture requests.

    A bounded width asks for its maximum; an unbounded width keeps the value's
    full precision; a presentation without a width asks for the number of
    digits it spells (the default one).
    """
    if width:
        minimum, maximum = _parse_width(width)
        if maximum is not None:
            return maximum
        del minimum
        return 6
    digits = sum(character.isdigit() for character in presentation)
    return digits or 1


class _RoundedFraction:
    """A value with one substituted microsecond field for the ``f`` marker.

    ``parse_datetime_marker`` reads the fractional component only through the
    ``microsecond`` attribute, so a narrow proxy lets the 2.0 rounding rule
    reuse the substrate's presentation grammar instead of a second copy.
    """

    __slots__ = ("_value", "microsecond")

    def __init__(self, value: Any, microsecond: int) -> None:
        self._value = value
        self.microsecond = microsecond

    def __getattr__(self, name: str) -> Any:
        return getattr(self._value, name)


def _am_pm(value: Any, presentation: str, width: str) -> str:
    """The ``P`` component in F&O's ``am``/``pm`` words.

    The substrate returns ``a.m.``/``p.m.``, which is not the XSLT 2.0 value.
    A width modifier caps the word (``[PNn,1-1]`` is ``A``); it never pads it,
    because the longest word is two characters.
    """
    word = "am" if value.hour < 12 else "pm"
    if presentation == "N":
        word = word.upper()
    elif presentation == "Nn":
        word = word.title()
    if width:
        _, maximum = _parse_width(width)
        if maximum is not None:
            word = word[:maximum]
    return word


def _timezone_component(value: Any, component: str, presentation: str, width: str) -> str | None:
    """The ``Z``/``z`` component, including the XSLT 2.0 errata form.

    With an empty presentation and a width that cannot hold ``:MM``, an
    integral-hour offset has no minute part: ``[z,2-2]`` is ``GMT-05`` for a
    whole-hour offset and ``GMT-05:30`` otherwise.  Every other presentation
    keeps the substrate's full ``-05:00`` form.
    """
    if component not in ("Z", "z"):
        return None
    if presentation:
        return None
    if not width:
        return None
    _, maximum = _parse_width(width)
    if maximum is None or maximum > 2:
        return None
    tzinfo = getattr(value, "tzinfo", None)
    offset = None if tzinfo is None else tzinfo.utcoffset(None)
    total = 0 if offset is None else offset.days * 1440 + offset.seconds // 60
    sign = "-" if total < 0 else "+"
    magnitude = abs(total)
    hours, minutes = divmod(magnitude, 60)
    text = f"{sign}{hours:02d}" if component == "Z" else f"GMT{sign}{hours:02d}"
    if minutes:
        text += f":{minutes:02d}"
    return text


def _is_eqname(value: str) -> bool:
    """Whether *value* is a syntactically valid EQName or lexical QName."""
    if value.startswith("Q{"):
        close = value.find("}")
        if close < 0:
            return False
        local = value[close + 1 :]
        return bool(local) and ":" not in local and QName.is_valid(f"x:{local}")
    return QName.is_valid(value)


def format_datetime_value(
    kind: str,
    value: Any,
    picture: str,
    language: str | None = None,
    calendar: str | None = None,
    place: str | None = None,
    *,
    round_fractional: bool = False,
) -> str:
    """Format one date, time, or dateTime value with an F&O picture string.

    *kind* is ``"date"``, ``"time"``, or ``"dateTime"``.  *value* is the
    engine's value for that type or ``None`` for an empty sequence.  An
    unsupported language or calendar is reported in the result with the
    implementation fallback (English and the proleptic Gregorian calendar),
    never treated as an error; a syntactically invalid calendar is FOFD1340.
    *round_fractional* selects the XSLT 2.0 rule for the ``f`` component
    (round to the requested precision); the XPath 3.1 rule truncates.
    """
    from elementpath.xpath30.xpath30_helpers import parse_datetime_marker, parse_datetime_picture

    if kind not in _DATETIME_CLASSES:
        raise ValueError(f"unknown date/time kind {kind!r}")
    if value is None:
        return ""
    datatype = _DATETIME_CLASSES[kind]
    if not isinstance(value, datatype):
        try:
            value = datatype.fromstring(str(value))
        except (TypeError, ValueError):
            raise xpath_error("XPTY0004", f"a {kind} value is required") from None

    literals, markers = _picture_parts(picture)
    invalid = _DATE_INVALID_COMPONENTS if kind == "date" else _TIME_INVALID_COMPONENTS
    if kind == "dateTime":
        invalid = frozenset()
    notes: list[str] = []
    if language is not None and language not in _IMPLEMENTED_LANGUAGES:
        language = "en"
        notes.append("Language: en")
    if calendar is not None and calendar not in _IMPLEMENTED_CALENDARS:
        if calendar.startswith("Q{}"):
            calendar = calendar[3:]
        if not _is_eqname(calendar):
            raise xpath_error("FOFD1340", f"Invalid calendar argument {calendar!r}")
        # An implementation-defined calendar is used instead; F&O requires the
        # result to name it so a caller can recognise the substitution.
        notes.append("Calendar: AD")
        calendar = "AD"
    for marker in markers:
        # Validate the presentation and width with the substrate's grammar so
        # both profiles report the same F&O diagnostic for a bad picture.
        parse_datetime_picture(f"[{marker}]")
        if marker and marker[0] in invalid:
            raise xpath_error(
                "FOFD1350",
                f"Invalid date formatting component '[{marker}]' for a {kind} value",
            )

    if place is not None:
        import zoneinfo

        try:
            zone = zoneinfo.ZoneInfo(place.strip())
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            raise xpath_error("FOFD1340", f"Invalid place argument {place!r}") from None
        value = value.astimezone(zone)

    if round_fractional:
        fractional = [_marker_parts(marker) for marker in markers if marker and marker[0] == "f"]
        if fractional:
            digits = max(
                _fractional_digits(presentation, width) for _, presentation, width in fractional
            )
            if digits < 6:
                factor = 10 ** (6 - digits)
                micro = ((value.microsecond + factor // 2) // factor) * factor
                if micro >= 1_000_000:
                    # The rounded fraction is a whole second: advance the
                    # value so the carry reaches seconds and the date.
                    from elementpath.datatypes import DayTimeDuration

                    value = value + DayTimeDuration(seconds=1)
                    micro = 0
                value = _RoundedFraction(value, micro)

    pieces: list[str] = []
    if notes:
        pieces.append("[" + ", ".join(notes) + "]")
    for index, marker in enumerate(markers):
        pieces.append(literals[index])
        component, presentation, width = _marker_parts(marker)
        formatted = _timezone_component(value, component, presentation, width)
        if formatted is None and component == "P":
            formatted = _am_pm(value, presentation, width)
        if formatted is None:
            # A roman numeral is computed from the unpadded value and then
            # measured against the width: the substrate applies the numeric
            # width before converting the digits to letters.
            source = marker[: -(len(width) + 1)] if width and presentation in ("i", "I") else marker
            formatted = parse_datetime_marker(f"[{source}]", value, language)
        if presentation in ("i", "I"):
            formatted = _with_presentation_width(formatted, component, presentation, width)
        pieces.append(formatted)
    pieces.append(literals[-1])
    return "".join(pieces)


def format_integer_value(value: int, picture: str) -> str:
    """Format one integer with an F&O 2.0 ``format-integer`` picture.

    The F&O 2.0 picture vocabulary is the decimal spelling, an alphabetic or
    roman sequence, or a word sequence, each with an optional ``;o``/``;c``
    modifier and optional grouping separators.  This is the native-2.0
    replacement for the XPath 2.0 parser's missing symbol; the XPath 3.1
    parser keeps its own implementation.
    """
    from elementpath.xpath30.xpath30_helpers import (
        DECIMAL_DIGIT_PATTERN,
        MODIFIER_PATTERN,
        UNICODE_DIGIT_PATTERN,
        int_to_alphabetic,
        int_to_roman,
        int_to_words,
        ordinal_suffix,
    )

    fmt_token, separator, fmt_modifier = picture.rpartition(";")
    if not separator:
        fmt_token, fmt_modifier = picture, ""
    if MODIFIER_PATTERN.match(fmt_modifier) is None:
        raise xpath_error("FODF1310", f"Invalid format modifier in picture {picture!r}")
    if not fmt_token:
        raise xpath_error("FODF1310", "the picture has no primary format token")

    if fmt_token in ("a", "A", "i", "I", "w", "W", "Ww"):
        if fmt_token == "a":
            result = int_to_alphabetic(value)
        elif fmt_token == "A":
            result = int_to_alphabetic(value).upper()
        elif fmt_token == "i":
            result = int_to_roman(value).lower()
        elif fmt_token == "I":
            result = int_to_roman(value)
        else:
            words = int_to_words(value, "en", fmt_modifier)
            if fmt_token == "W":
                return words.upper()
            if fmt_token == "Ww":
                return words.title()
            return words
    else:
        from elementpath.xpath30.xpath30_helpers import format_digits

        digits = UNICODE_DIGIT_PATTERN.findall(fmt_token)
        if DECIMAL_DIGIT_PATTERN.search(fmt_token) is None or not digits:
            raise xpath_error("FODF1310", "Invalid primary format token in picture")
        zero = ord(digits[0])
        if any((ord(character) - zero) > 10 for character in digits[1:]):
            raise xpath_error("FODF1310", "The picture mixes digits from different families")
        if fmt_token[0].isdigit() and "#" in fmt_token:
            raise xpath_error("FODF1310", "Invalid primary format token in picture")
        if not fmt_token[0].isdigit() and fmt_token[0] != "#":
            raise xpath_error("FODF1310", "Invalid grouping in picture")
        family_zero = zero
        while chr(family_zero - 1).isdigit():
            family_zero -= 1
        family = "".join(chr(family_zero + offset) for offset in range(10))
        signed = str(abs(value))
        if value < 0:
            result = "-" + format_digits(signed, fmt_token, family)
        else:
            result = format_digits(signed, fmt_token, family)

    if fmt_modifier.startswith("o"):
        return f"{result}{ordinal_suffix(value)}"
    return result
