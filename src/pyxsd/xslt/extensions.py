"""Immutable registry of Python extension functions, instructions, collations.

An :class:`XsltExtensions` value is the only seam through which host Python
code enters an XSLT transformation.  Registration returns a new registry; a
compiled stylesheet keeps the snapshot it was compiled with, so a later
registration never changes compiled expressions, ``function-available``
answers, or a running transformation.  Stylesheet strings never identify a
Python module: there is no import-path loading and no process-global callback
slot.

Instruction registrations name the application's own extension elements.  A
stylesheet element in a namespace declared under ``extension-element-prefixes``
is an instruction candidate: it compiles through the registered family
compiler when the compilation carries one, runs its ``xsl:fallback`` children
when the name is not registered, and is a static error without a fallback.
An element outside any declared prefix keeps the ordinary literal-result
handling.

Callbacks are non-contextual by default.  A contextual callback receives the
current context item as its first argument, followed by the declared
parameters.  Arguments and results cross one explicit typed boundary:

* ``xs:integer`` is a Python ``int``, ``xs:decimal`` a ``Decimal``,
  ``xs:double``/``xs:float`` a ``float``, ``xs:boolean`` a ``bool``, and the
  string family a ``str``.  ``xs:QName``/``xs:NOTATION`` keep the
  :class:`~pyxsd.xdm.XdmAtomic` so namespace identity survives.
* Nodes stay :class:`~pyxsd.xdm.nodes.XdmNode`; maps, arrays, and functions stay
  :class:`~pyxsd.xdm.XdmMap`, :class:`~pyxsd.xdm.XdmArray`, and
  :class:`~pyxsd.xdm.XdmFunction`.
* ``?`` yields ``None`` for an empty argument and ``()`` for an empty result;
  ``*``/``+`` yield tuples; a required singleton yields one item.
* ``None`` is always an error (``XPTY0004``), never the empty sequence.

Custom collations participate in ``xsl:sort``, ``xsl:for-each-group``, and
``xsl:merge`` ordering through pyxsd's :class:`Collation` seam.  General XPath
string functions (``compare()``, ``contains()``, and the like) still accept
only the collations the underlying XPath engine recognizes; a custom URI in
those positions is a ledgered boundary rather than a silent fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit

from elementpath.helpers import is_ncname

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmItem, XdmMap, XdmSequence, as_xdm
from pyxsd.xdm.annotations import builtin_type_classes
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xdm.values import _is_xsd_subtype, _local_type_name
from pyxsd.xpath.functions import XdmFunctionTarget, call_host_function
from pyxsd.xslt.compiler.dispatch import InstructionCompiler
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.python_rules import PYTHON_RULES_NS, PythonRules
from pyxsd.xslt.runtime.dispatch import PlanExecutor

__all__ = [
    "EXTENSION_ERROR_CODE",
    "NO_ARGS",
    "XS_INTEGER_TO_INTEGER",
    "ExtensionFunction",
    "ExtensionInstruction",
    "ExtensionSignature",
    "ExtensionTarget",
    "XsltExtensions",
    "no_args",
    "xs_integer_to_integer",
]

#: The XPath type error used for every extension argument/result mismatch.
EXTENSION_ERROR_CODE: Final = "XPTY0004"

_OCCURRENCES: Final = {
    "": "one",
    "?": "zero-or-one",
    "*": "zero-or-more",
    "+": "one-or-more",
}
_STRUCTURAL_TYPES: Final = frozenset(
    {
        "item()",
        "node()",
        "element()",
        "document-node()",
        "map(*)",
        "array(*)",
        "function(*)",
    }
)
_ATOMIC_LOCALS: Final = frozenset(builtin_type_classes()) | {"untypedAtomic"}
_STRING_LOCALS: Final = frozenset(
    {
        "string",
        "normalizedString",
        "token",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "ID",
        "IDREF",
        "ENTITY",
        "untypedAtomic",
        "anyURI",
    }
)
_NUMERIC_RANKS: Final = {
    "integer": 0,
    "nonPositiveInteger": 0,
    "negativeInteger": 0,
    "long": 0,
    "int": 0,
    "short": 0,
    "byte": 0,
    "nonNegativeInteger": 0,
    "unsignedLong": 0,
    "unsignedInt": 0,
    "unsignedShort": 0,
    "unsignedByte": 0,
    "positiveInteger": 0,
    "decimal": 1,
    "float": 2,
    "double": 3,
}


@dataclass(frozen=True, slots=True)
class _SequenceType:
    """One parsed ``as``-style extension parameter or result type."""

    type_name: str
    occurrence: str = "one"

    @property
    def local(self) -> str | None:
        return self.type_name.partition(":")[2] if self.type_name.startswith("xs:") else None


def _parse_sequence_type(value: str) -> _SequenceType:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"unsupported extension sequence type {value!r}")
    occurrence = _OCCURRENCES.get(value[-1], "")
    base = value[:-1] if occurrence or value[-1] in "?*+" else value
    if base in _STRUCTURAL_TYPES:
        return _SequenceType(base, occurrence or "one")
    local = base.partition(":")[2] if ":" in base else base
    namespace = base.partition(":")[0] if ":" in base else ""
    if namespace not in ("", "xs") or local not in _ATOMIC_LOCALS:
        raise ValueError(f"unsupported extension sequence type {value!r}")
    return _SequenceType(f"xs:{local}", occurrence or "one")


@dataclass(frozen=True, slots=True)
class ExtensionSignature:
    """Declared parameter and result sequence types for one extension."""

    parameters: tuple[str, ...] = ()
    result: str = "item()*"

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, tuple):
            raise TypeError("extension parameters must be a tuple of sequence types")
        for parameter in self.parameters:
            _parse_sequence_type(parameter)
        _parse_sequence_type(self.result)

    @property
    def parsed_parameters(self) -> tuple[_SequenceType, ...]:
        return tuple(_parse_sequence_type(parameter) for parameter in self.parameters)

    @property
    def parsed_result(self) -> _SequenceType:
        return _parse_sequence_type(self.result)


NO_ARGS: Final = ExtensionSignature()
XS_INTEGER_TO_INTEGER: Final = ExtensionSignature(("xs:integer",), "xs:integer")

#: Lower-case aliases matching the plan's example spelling.
no_args: Final = NO_ARGS
xs_integer_to_integer: Final = XS_INTEGER_TO_INTEGER


def _normalise_expanded_name(name: str) -> str:
    """Canonicalise an expanded name, rejecting malformed or lexical forms."""
    if not isinstance(name, str) or not name:
        raise ValueError("a Python extension name must be an expanded name")
    if name.startswith("Q{"):
        name = "{" + name[2:]
    if name.startswith("{"):
        uri, separator, local = name[1:].partition("}")
        if not separator or not uri or not is_ncname(local):
            raise ValueError(f"{name!r} is not a valid expanded name")
        return f"{{{uri}}}{local}"
    raise ValueError(f"{name!r} is not an expanded name; use {{uri}}local or Q{{uri}}local")


def _registration_name(name: str) -> str:
    """Validate a registration name, allowing a lexical prefixed QName."""
    if not isinstance(name, str) or not name:
        raise ValueError("a Python extension name must be an expanded name")
    if name.startswith("Q{") or name.startswith("{"):
        return _normalise_expanded_name(name)
    prefix, separator, local = name.partition(":")
    if not separator or not is_ncname(prefix) or not is_ncname(local):
        raise ValueError(f"{name!r} is not an expanded name or a prefixed QName")
    return name


def _expanded_name(
    name: str,
    namespaces: Mapping[str, str],
    *,
    identity: str | None,
) -> str:
    if name.startswith("{"):
        return name
    prefix, _, local = name.partition(":")
    uri = namespaces.get(prefix)
    if uri is None:
        raise XsltStaticError(
            f"the Python extension function {name!r} uses an unbound namespace prefix",
            code="XTSE0280",
            uri=identity,
        )
    return f"{{{uri}}}{local}"


@dataclass(frozen=True, slots=True)
class ExtensionFunction:
    """One registered Python callback and its typed signature."""

    name: str
    arity: int
    callback: Callable[..., Any]
    signature: ExtensionSignature = NO_ARGS
    contextual: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _registration_name(self.name))
        if isinstance(self.arity, bool) or not isinstance(self.arity, int) or self.arity < 0:
            raise ValueError("a Python extension arity must be a non-negative integer")
        if not callable(self.callback):
            raise TypeError("a Python extension requires a callable callback")
        if not isinstance(self.signature, ExtensionSignature):
            raise TypeError("a Python extension signature must be an ExtensionSignature")
        if len(self.signature.parameters) != self.arity:
            raise ValueError(
                f"the signature for {self.name!r} declares "
                f"{len(self.signature.parameters)} parameters but the arity is {self.arity}"
            )
        if not isinstance(self.contextual, bool):
            raise TypeError("contextual must be a boolean")


def _type_error(
    name: str,
    label: str,
    expected: str,
    sequence: Sequence[XdmItem],
) -> XsltDynamicError:
    actual = "empty sequence" if not sequence else type(sequence[0]).__name__
    return XsltDynamicError(
        f"the Python extension function {name!r} {label} must be {expected}, got {actual}",
        code=EXTENSION_ERROR_CODE,
    )


def _argument_item(
    item: XdmItem,
    expected: _SequenceType,
    name: str,
    label: str,
) -> Any:
    type_name = expected.type_name
    if type_name == "item()":
        return item
    if type_name == "node()":
        if not isinstance(item, XdmNode):
            raise _type_error(name, label, type_name, (item,))
        return item
    if type_name == "element()":
        if not isinstance(item, XdmNode) or item.kind is not NodeKind.ELEMENT:
            raise _type_error(name, label, type_name, (item,))
        return item
    if type_name == "document-node()":
        if not isinstance(item, XdmNode) or item.kind is not NodeKind.DOCUMENT:
            raise _type_error(name, label, type_name, (item,))
        return item
    if type_name == "map(*)":
        if not isinstance(item, XdmMap):
            raise _type_error(name, label, type_name, (item,))
        return item
    if type_name == "array(*)":
        if not isinstance(item, XdmArray):
            raise _type_error(name, label, type_name, (item,))
        return item
    if type_name == "function(*)":
        if not isinstance(item, XdmFunction):
            raise _type_error(name, label, type_name, (item,))
        return item
    if not isinstance(item, XdmAtomic):
        raise _type_error(name, label, type_name, (item,))
    assert expected.local is not None
    if not _atomic_matches(item, expected.local):
        raise _type_error(name, label, type_name, (item,))
    return _atomic_payload(item, expected.local)


def _atomic_matches(item: XdmAtomic, expected: str) -> bool:
    actual = _local_type_name(item.type_name)
    if _is_xsd_subtype(actual, expected):
        return True
    if expected in _STRING_LOCALS and actual in _STRING_LOCALS:
        return True
    actual_rank = _NUMERIC_RANKS.get(actual)
    expected_rank = _NUMERIC_RANKS.get(expected)
    return actual_rank is not None and expected_rank is not None and actual_rank <= expected_rank


def _atomic_payload(item: XdmAtomic, expected: str) -> Any:
    if _is_xsd_subtype(expected, "integer") or expected == "integer":
        return int(item.value)
    if expected == "decimal":
        value = item.value
        return Decimal(value) if isinstance(value, Decimal) else Decimal(str(value))
    if expected in ("float", "double"):
        return float(item.value)
    if expected == "boolean":
        return bool(item.value)
    if expected in _STRING_LOCALS or _is_xsd_subtype(expected, "string"):
        return str(item.value)
    return item


def _argument_sequence(
    sequence: Sequence[XdmItem],
    expected: _SequenceType,
    name: str,
    label: str,
) -> Any:
    values = tuple(sequence)
    occurrence = expected.occurrence
    if occurrence == "one" and len(values) != 1:
        raise _type_error(name, label, expected.type_name, values)
    if occurrence == "zero-or-one":
        if not values:
            return None
        if len(values) != 1:
            raise _type_error(name, label, expected.type_name, values)
    elif occurrence == "one-or-more" and not values:
        raise _type_error(name, label, expected.type_name, values)
    converted = tuple(_argument_item(item, expected, name, label) for item in values)
    if occurrence in ("one", "zero-or-one"):
        return converted[0]
    return converted


def _result_item(
    item: XdmItem,
    expected: _SequenceType,
    name: str,
) -> XdmItem:
    type_name = expected.type_name
    if type_name == "item()":
        return item
    if type_name == "node()" and isinstance(item, XdmNode):
        return item
    if type_name == "element()" and isinstance(item, XdmNode) and item.kind is NodeKind.ELEMENT:
        return item
    if (
        type_name == "document-node()"
        and isinstance(item, XdmNode)
        and item.kind is NodeKind.DOCUMENT
    ):
        return item
    if type_name == "map(*)" and isinstance(item, XdmMap):
        return item
    if type_name == "array(*)" and isinstance(item, XdmArray):
        return item
    if type_name == "function(*)" and isinstance(item, XdmFunction):
        return item
    if type_name.startswith("xs:") and isinstance(item, XdmAtomic) and expected.local is not None:
        if _is_xsd_subtype(_local_type_name(item.type_name), expected.local):
            return item
        try:
            return item.cast(type_name)
        except (TypeError, ValueError) as exc:
            raise XsltDynamicError(
                f"the Python extension function {name!r} result must be {type_name}, "
                f"got {type(item).__name__}",
                code=EXTENSION_ERROR_CODE,
                cause=exc,
            ) from exc
    raise XsltDynamicError(
        f"the Python extension function {name!r} result must be {type_name}, "
        f"got {type(item).__name__}",
        code=EXTENSION_ERROR_CODE,
    )


def _result_sequence(
    sequence: XdmSequence,
    expected: _SequenceType,
    name: str,
) -> XdmSequence:
    occurrence = expected.occurrence
    count = len(sequence)
    if occurrence == "one" and count != 1:
        raise _type_error(name, "result", expected.type_name, sequence)
    if occurrence == "zero-or-one" and count > 1:
        raise _type_error(name, "result", expected.type_name, sequence)
    if occurrence == "one-or-more" and count == 0:
        raise _type_error(name, "result", expected.type_name, sequence)
    return tuple(_result_item(item, expected, name) for item in sequence)


def _invoke(
    definition: ExtensionFunction,
    context_item: XdmItem | None,
    arguments: Sequence[XdmSequence],
) -> XdmSequence:
    name = definition.name
    converted = tuple(
        _argument_sequence(sequence, parameter, name, f"argument {index + 1}")
        for index, (sequence, parameter) in enumerate(
            zip(arguments, definition.signature.parsed_parameters, strict=True)
        )
    )
    call_arguments = (context_item, *converted) if definition.contextual else converted
    try:
        result = definition.callback(*call_arguments)
    except Exception as exc:
        raise XsltDynamicError(
            f"the Python extension function {name!r} raised {type(exc).__name__}: {exc}",
            code=None,
            allow_catch=False,
            cause=exc,
        ) from exc
    if result is None:
        raise XsltDynamicError(
            f"the Python extension function {name!r} returned None; "
            "return () for the empty sequence",
            code=EXTENSION_ERROR_CODE,
        )
    try:
        sequence = as_xdm(result)
    except (TypeError, ValueError) as exc:
        raise XsltDynamicError(
            f"the Python extension function {name!r} returned an unusable result: {exc}",
            code=EXTENSION_ERROR_CODE,
            cause=exc,
        ) from exc
    return _result_sequence(sequence, definition.signature.parsed_result, name)


@dataclass(frozen=True, slots=True)
class ExtensionTarget(XdmFunctionTarget):
    """A runtime target that validates XDM sequences before the callback."""

    definitions: tuple[ExtensionFunction, ...]
    context_item: XdmItem | None = None
    fallback: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.definitions:
            raise ValueError("an extension target requires at least one function")
        if any(not isinstance(item, ExtensionFunction) for item in self.definitions):
            raise TypeError("extension targets require ExtensionFunction definitions")
        if self.context_item is not None and not isinstance(self.context_item, XdmItem):
            raise TypeError("an extension context item must be an XDM item or None")
        if self.fallback is not None and not callable(self.fallback):
            raise TypeError("an extension fallback must be callable or None")

    def with_context(self, context_item: XdmItem | None) -> ExtensionTarget:
        """Return this target with one live context item for a direct call."""
        return replace(self, context_item=context_item)

    def invoke_xdm(
        self,
        arguments: tuple[XdmSequence, ...],
        environment: Any = None,
    ) -> XdmSequence:
        del environment
        definition = next(
            (item for item in self.definitions if item.arity == len(arguments)),
            None,
        )
        if definition is None:
            fallback = self.fallback
            if fallback is not None:
                return call_host_function(fallback, arguments)
            name = self.definitions[0].name
            raise XsltDynamicError(
                f"the Python extension function {name!r} has no arity {len(arguments)} overload",
                code=EXTENSION_ERROR_CODE,
            )
        return _invoke(definition, self.context_item, arguments)


@dataclass(frozen=True, slots=True)
class ExtensionInstruction:
    """One registered application instruction and its executor.

    *name* is the instruction's expanded QName (``{uri}local``); *compiler*
    compiles the element into the application's own plan on this compilation's
    static context, and *executor* executes that plan through the compiled
    artifact's per-artifact snapshot.  Both are application-supplied
    callables; the engine never inspects the plan type at registration time,
    so the executor binding is discovered when the first instruction of the
    family is compiled.
    """

    name: str
    compiler: InstructionCompiler
    executor: PlanExecutor

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalise_expanded_name(self.name))
        if not callable(self.compiler):
            raise TypeError("an extension instruction requires a callable compiler")
        if not callable(self.executor):
            raise TypeError("an extension instruction requires a callable executor")


@dataclass(frozen=True, slots=True)
class XsltExtensions:
    """An immutable registry of extension functions, instructions, collations.

    Instruction registrations (:meth:`register_instruction`) declare trusted
    application extension elements the compiler recognizes before
    literal-result handling; each carries the application's own compiler and
    plan executor and is captured on the compiled artifact it was compiled
    with.
    """

    _functions: Mapping[tuple[str, int], ExtensionFunction] = field(
        default_factory=dict, repr=False
    )
    _collations: Mapping[str, Any] = field(default_factory=dict, repr=False)
    _python_rules: PythonRules = field(default_factory=PythonRules.empty, repr=False)
    _instructions: Mapping[str, ExtensionInstruction] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        functions: dict[tuple[str, int], ExtensionFunction] = {}
        for key, definition in self._functions.items():
            if not isinstance(definition, ExtensionFunction):
                raise TypeError("extension registries map names to ExtensionFunction values")
            if key != (definition.name, definition.arity):
                raise ValueError("an extension registry key must match its definition")
            functions[key] = definition
        object.__setattr__(self, "_functions", MappingProxyType(functions))
        collations = dict(self._collations)
        from pyxsd.xslt.runtime.iteration import Collation

        for uri, collation in collations.items():
            if not isinstance(uri, str) or not isinstance(collation, Collation):
                raise TypeError("a collation registry maps URIs to Collation values")
        object.__setattr__(self, "_collations", MappingProxyType(collations))
        if not isinstance(self._python_rules, PythonRules):
            raise TypeError("python_rules must be a PythonRules registry")
        instructions = dict(self._instructions)
        for instruction_name, registered in instructions.items():
            if not isinstance(registered, ExtensionInstruction):
                raise TypeError("instruction registries map names to ExtensionInstruction values")
            if instruction_name != registered.name:
                raise ValueError("an instruction registry key must match its definition")
        object.__setattr__(self, "_instructions", MappingProxyType(instructions))

    @property
    def functions(self) -> Mapping[tuple[str, int], ExtensionFunction]:
        """Registered functions keyed by expanded name and arity."""
        return self._functions

    @property
    def collations(self) -> Mapping[str, Any]:
        """Registered collations keyed by absolute URI."""
        return self._collations

    @property
    def python_rules(self) -> PythonRules:
        """The immutable Python customization rules captured at compile time."""
        return self._python_rules

    @property
    def instructions(self) -> Mapping[str, ExtensionInstruction]:
        """Registered instructions keyed by expanded QName."""
        return self._instructions

    def with_python_rules(self, rules: PythonRules) -> XsltExtensions:
        """Return a new registry carrying *rules*."""
        if not isinstance(rules, PythonRules):
            raise TypeError("python_rules must be a PythonRules registry")
        return replace(self, _python_rules=rules)

    def function(
        self,
        name: str,
        arity: int,
        signature: ExtensionSignature = NO_ARGS,
    ) -> Callable[..., XsltExtensions]:
        """Return a registrar for one function, preserving immutability."""

        def register(
            callback: Callable[..., Any],
            *,
            contextual: bool = False,
        ) -> XsltExtensions:
            return self.register_function(
                name,
                arity,
                callback,
                signature,
                contextual=contextual,
            )

        return register

    def register_function(
        self,
        name: str,
        arity: int,
        callback: Callable[..., Any],
        signature: ExtensionSignature = NO_ARGS,
        *,
        contextual: bool = False,
    ) -> XsltExtensions:
        """Return a new registry with one Python callback registered."""
        definition = ExtensionFunction(
            name=_registration_name(name),
            arity=arity,
            callback=callback,
            signature=signature,
            contextual=contextual,
        )
        key = (definition.name, definition.arity)
        if key in self._functions:
            raise ValueError(
                f"the Python extension function {definition.name!r} with arity "
                f"{definition.arity} is already registered"
            )
        return replace(self, _functions={**self._functions, key: definition})

    def collation(
        self,
        uri: str,
        *,
        compare: Callable[[str, str], int] | None = None,
        sort_key: Callable[[str], Any] | None = None,
    ) -> XsltExtensions:
        """Return a new registry with one custom collation registered."""
        if not isinstance(uri, str) or not urlsplit(uri).scheme:
            raise ValueError("a custom collation URI must be absolute")
        if compare is not None and not callable(compare):
            raise TypeError("a custom collation compare must be callable or None")
        if sort_key is not None and not callable(sort_key):
            raise TypeError("a custom collation sort_key must be callable or None")
        if compare is None and sort_key is None:
            raise ValueError("a custom collation requires a compare or sort_key callable")
        if uri in self._collations:
            raise ValueError(f"the custom collation {uri!r} is already registered")
        from pyxsd.xslt.runtime.iteration import Collation

        collation = Collation(
            uri,
            kind="custom",
            custom_compare=compare,
            sort_key=sort_key,
        )
        return replace(self, _collations={**self._collations, uri: collation})

    def register_instruction(
        self,
        name: str,
        *,
        compiler: InstructionCompiler,
        executor: PlanExecutor,
    ) -> XsltExtensions:
        """Return a new registry with one application instruction registered.

        *name* must be an expanded QName in the application's own namespace;
        the private ``urn:pyxsd:python-rules`` namespace belongs to the
        generated customization layer and can never be registered.  The
        compiled artifact keeps this registration as an immutable
        per-compilation and per-artifact snapshot, so a later registration
        never changes an artifact that was compiled without it.
        """
        registered = ExtensionInstruction(name=name, compiler=compiler, executor=executor)
        if registered.name.startswith(f"{{{PYTHON_RULES_NS}}}"):
            raise ValueError(
                f"the private Python instruction namespace {PYTHON_RULES_NS!r} is "
                "reserved for the generated customization layer"
            )
        if registered.name in self._instructions:
            raise ValueError(f"the extension instruction {registered.name!r} is already registered")
        return replace(self, _instructions={**self._instructions, registered.name: registered})

    def function_available(self, name: str, arity: int | None = None) -> bool:
        """Whether the registry declares *name* (an expanded or lexical QName)."""
        try:
            expanded = _normalise_expanded_name(name)
        except ValueError:
            try:
                expanded = _registration_name(name)
            except ValueError:
                return False
        if arity is None:
            return any(function_name == expanded for function_name, _ in self._functions)
        return (expanded, arity) in self._functions

    def availability(self) -> frozenset[tuple[str, int]]:
        """The expanded-name/arity pairs of every registered function."""
        return frozenset(self._functions)

    def resolve(
        self,
        namespaces: Mapping[str, str],
        *,
        identity: str | None = None,
    ) -> XsltExtensions:
        """Resolve every lexical registration against one stylesheet scope."""
        resolved: dict[tuple[str, int], ExtensionFunction] = {}
        for (name, arity), definition in self._functions.items():
            expanded = _expanded_name(name, namespaces, identity=identity)
            key = (expanded, arity)
            if key in resolved:
                raise XsltStaticError(
                    f"the Python extension function {key[0]!r} with arity {arity} "
                    "is registered twice",
                    code="XTSE0010",
                    uri=identity,
                )
            resolved[key] = replace(definition, name=expanded)
        return XsltExtensions(resolved, self._collations, self._python_rules, self._instructions)

    def runtime_targets(
        self,
        context_item: XdmItem | None = None,
        *,
        existing: Mapping[str, Any] | None = None,
    ) -> Mapping[str, ExtensionTarget]:
        """Runtime targets keyed by expanded function name.

        *existing* supplies the stylesheet-function dispatchers already in the
        environment.  A name registered both as an extension and as a
        stylesheet function keeps both arities: extension arities route to the
        Python callback and every other arity routes to the existing
        dispatcher.
        """
        grouped: dict[str, list[ExtensionFunction]] = {}
        for definition in self._functions.values():
            grouped.setdefault(definition.name, []).append(definition)
        targets: dict[str, ExtensionTarget] = {}
        for name, definitions in grouped.items():
            fallback = None if existing is None else existing.get(name)
            if isinstance(fallback, ExtensionTarget):
                fallback = fallback.fallback
            targets[name] = ExtensionTarget(tuple(definitions), context_item, fallback)
        return MappingProxyType(targets)
