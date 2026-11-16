"""RELAX NG datatype protocols and the built-in/XSD 1.0 libraries.

RELAX NG delegates lexical acceptance and value equality to datatype
libraries.  A factory compiles a type name, its parameters, and the
schema-side context into a reusable :class:`Datatype`.  Every operation
receives the context of the string it interprets, so a ``value`` pattern can
compare a schema-side QName against an instance-side QName in different
prefix scopes.

The empty library URI is the RELAX NG built-in library.  It has exactly the
two datatypes of RELAX NG 1.0 §6.2.9: ``string`` accepts every string and
compares by identity, and ``token`` accepts every string and compares after
XML-whitespace normalization.  Parameters are not allowed.

:data:`XSD_LIBRARY` adapts pyxsd's existing XSD built-in classes.  The
adapter follows XSD 1.0 semantics, not the XSD compiler's default 1.1
profile:

* the XSD 1.1-only types ``anyAtomicType``, ``dateTimeStamp``,
  ``dayTimeDuration``, ``yearMonthDuration`` (and ``precisionDecimal``) are
  rejected explicitly rather than accepted as strings;
* lexical year ``0000`` is rejected for ``date``, ``dateTime``, ``gYear``
  and ``gYearMonth``;
* duration equality uses the six-dimensional XSD 1.0 value space, so
  ``P1Y`` and ``P12M`` are distinct values.

Duration ordering inside facet bounds still uses the shared facet helper, as
does negative-year calendar validation.  The shared pyxsd datatype classes
implement the XSD 1.1 numbering for negative years (and the XSD 1.1 reading
of ``xs:anyURI``); the adapter does not reinterpret existing negative-year
dates.  Those limitations are documented rather than hidden by a fallback.

The XSD adapter rejects the ``whiteSpace`` and ``enumeration`` parameters as
the RELAX NG datatype guidelines require.  Repeated ``pattern`` parameters
are intersected; every other parameter may appear at most once.  Facets are
validated when the datatype is compiled, and an inapplicable facet is a
parameter error.

``allows``, ``equal``, ``decode`` and ``encode`` are independent operations.
``decode`` and ``encode`` are optional pyxsd capabilities, not RELAX NG
conformance: a custom datatype may implement only validation, in which case
the inherited methods raise :class:`UnsupportedDatatypeOperation`.  Value
equality deliberately ignores facet parameters because RELAX NG's
``datatypeEqual`` service has no parameters.

A compiled datatype is frozen, hashable, and value-comparable.  Its ``repr``
is deterministic because the canonical choice order of the pattern IR sorts
leaves by ``repr``.  The schema-side :class:`DatatypeContext` is retained for
later ``value`` matching; for context-insensitive types it does not
participate in datatype equality, while QName and NOTATION datatypes compare
by their schema-side namespace bindings.
"""

from __future__ import annotations

import base64
import decimal
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType, NotImplementedType
from typing import Any, cast

from pyxsd import xsd_data_types as _xsd_types
from pyxsd.exceptions import PyXSDError
from pyxsd.facets import FacetConstraints, build_constraints, compile_pattern, whitespace_transform
from pyxsd.xsd_data_types import (
    _DURATION_PARTS,
    Base64Binary,
    Boolean,
    Decimal,
    Double,
    Duration,
    HexBinary,
    Integer,
    QName,
    String,
    Token,
    XsdDataType,
    qname_context,
    xsd_value_key,
)

__all__ = [
    "BUILTIN_FINGERPRINT",
    "BUILTIN_LIBRARY",
    "XSD_FINGERPRINT",
    "XSD_LIBRARY",
    "BuiltinDatatypeFactory",
    "Datatype",
    "DatatypeContext",
    "DatatypeError",
    "DatatypeFactory",
    "DatatypeLibraryRegistration",
    "DatatypeLibraryRegistry",
    "DatatypeParameter",
    "DatatypeParameterError",
    "DatatypeRegistrationError",
    "DatatypeSelector",
    "ScalarDatatype",
    "UnsupportedDatatypeError",
    "UnsupportedDatatypeOperation",
    "XsdDatatypeFactory",
    "resolve_datatype_selector",
]

#: The empty URI identifies RELAX NG's built-in datatype library.
BUILTIN_LIBRARY = ""

#: The URI of the XSD datatype library described by the RELAX NG guidelines.
XSD_LIBRARY = "http://www.w3.org/2001/XMLSchema-datatypes"

#: Fixed identities of the built-in libraries.  Bump these when their
#: observable behavior changes so retained candidate identities do not lie.
BUILTIN_FINGERPRINT = "pyxsd.relaxng.builtin/1"
XSD_FINGERPRINT = "pyxsd.relaxng.xsd-1.0/1"

_XSD_SHORTHAND = "xsd:"

#: Every XSD 1.0 built-in simple datatype, in the XSD 1.0 inventory.  The
#: class mapping is built from these names so a later XSD 1.1 class cannot
#: silently join the XSD 1.0 adapter.
_XSD_10_TYPE_NAMES = frozenset(
    {
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "NOTATION",
        "Name",
        "QName",
        "anySimpleType",
        "anyURI",
        "base64Binary",
        "boolean",
        "byte",
        "date",
        "dateTime",
        "decimal",
        "double",
        "duration",
        "float",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "hexBinary",
        "int",
        "integer",
        "language",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "normalizedString",
        "positiveInteger",
        "short",
        "string",
        "time",
        "token",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)

_XSD_11_ONLY_TYPE_NAMES = frozenset(
    {"anyAtomicType", "dateTimeStamp", "dayTimeDuration", "precisionDecimal", "yearMonthDuration"}
)

_XSD_10_TYPES: dict[str, type[XsdDataType]] = {}
for _module_value in list(vars(_xsd_types).values()):
    if (
        isinstance(_module_value, type)
        and _module_value.__module__ == _xsd_types.__name__
        and isinstance(_module_value.__dict__.get("name"), str)
        and _module_value.__dict__["name"] in _XSD_10_TYPE_NAMES
    ):
        _XSD_10_TYPES[_module_value.__dict__["name"]] = _module_value
del _module_value

#: A selector is either a bare built-in type name (or the ``xsd:`` shorthand)
#: or an explicit ``(library_uri, type_name)`` pair.
DatatypeSelector = str | tuple[str, str]

_QNAME_TYPES = frozenset({"QName", "NOTATION"})
_YEAR_ZERO_TYPES = frozenset({"date", "dateTime", "gYear", "gYearMonth"})
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_XSD11_YEAR_ZERO = re.compile(r"^-?0{4}(?![0-9])")
_PROCESS_DEPENDENT_FINGERPRINT = re.compile(r"0x[0-9a-fA-F]+")

_XSD_PARAMETER_NAMES = frozenset(
    {
        "fractionDigits",
        "length",
        "maxExclusive",
        "maxInclusive",
        "maxLength",
        "minExclusive",
        "minInclusive",
        "minLength",
        "pattern",
        "totalDigits",
    }
)
_REJECTED_XSD_PARAMETERS = frozenset({"enumeration", "whiteSpace"})
_BOUND_PARAMETER_NAMES = frozenset({"minInclusive", "maxInclusive", "minExclusive", "maxExclusive"})

#: XSD 1.0 types with an order relation, so the four bound facets apply.
#: Boolean, the string family, and the binary types are unordered.
_ORDERED_TYPE_NAMES = frozenset(
    {
        "byte",
        "date",
        "dateTime",
        "decimal",
        "double",
        "duration",
        "float",
        "gDay",
        "gMonth",
        "gMonthDay",
        "gYear",
        "gYearMonth",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "time",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)

#: The XSD facets read by :func:`pyxsd.facets.build_constraints` from a
#: SimpleType representative.  A parameter carrier exposes the same names.
_FACET_NAMES = (
    "length",
    "minLength",
    "maxLength",
    "minInclusive",
    "maxInclusive",
    "minExclusive",
    "maxExclusive",
    "totalDigits",
    "fractionDigits",
)


class DatatypeError(PyXSDError):
    """Base class for datatype capability and parameter failures."""


class UnsupportedDatatypeError(DatatypeError):
    """A datatype library or type is outside the adapter's capabilities."""


class DatatypeParameterError(DatatypeError, ValueError):
    """A datatype parameter is unknown, inapplicable, or inconsistent."""


class DatatypeRegistrationError(DatatypeError, ValueError):
    """A datatype-library registration is invalid or would replace an identity."""


class UnsupportedDatatypeOperation(DatatypeError):
    """The datatype has no decoder or encoder for the requested operation."""


class _NamespaceMap(Mapping[str, str]):
    """An immutable, hashable, deterministically ordered namespace map."""

    __slots__ = ("_items", "_mapping")

    def __init__(self, values: Mapping[str, str]) -> None:
        items = tuple(sorted(dict(values).items()))
        self._items = items
        self._mapping = MappingProxyType(dict(items))

    def __getitem__(self, key: str) -> str:
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def __eq__(self, other: object) -> bool | NotImplementedType:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(self._items) == dict(other)

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        return repr(dict(self._items))


@dataclass(frozen=True)
class DatatypeContext:
    """The schema- or instance-side context a lexical value is read in.

    ``base_uri`` is retained for datatypes that resolve relative references.
    ``namespaces`` maps in-scope prefixes to namespace URIs; the empty prefix
    is the default namespace.  The mapping is copied into an immutable,
    hashable form at construction.
    """

    base_uri: str | None
    namespaces: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespaces", _NamespaceMap(self.namespaces))


@dataclass(frozen=True)
class DatatypeParameter:
    """One RELAX NG datatype parameter: an XSD facet name and its lexical value."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a datatype parameter name must be a non-empty string")
        if not isinstance(self.value, str):
            raise ValueError("a datatype parameter value must be a string")


class Datatype(ABC):
    """The compiled behavior of one RELAX NG datatype.

    Implementations that serve as pattern payloads must also be immutable,
    hashable, value-comparable, and deterministically ``repr``-able, because
    the pattern IR interns and canonically orders them.
    """

    @abstractmethod
    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        """Return whether *lexical* is a legal value in *context*."""

    @abstractmethod
    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        """Return whether two lexical values denote the same datatype value."""

    def decode(self, lexical: str, context: DatatypeContext) -> Any:
        """Decode *lexical* into a Python value, when the datatype supports it."""
        raise UnsupportedDatatypeOperation(f"{type(self).__name__} does not provide a decoder")

    def encode(self, value: Any, context: DatatypeContext) -> str:
        """Encode a Python value as a lexical form, when the datatype supports it."""
        raise UnsupportedDatatypeOperation(f"{type(self).__name__} does not provide an encoder")


class DatatypeFactory(ABC):
    """A datatype library: compile a type name and parameters into a datatype."""

    @abstractmethod
    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        """Compile one datatype or report why it is unavailable."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


@dataclass(frozen=True)
class ScalarDatatype(Datatype):
    """A frozen scalar datatype backed by one pyxsd XSD built-in class.

    ``context`` is the schema-side context retained for later ``value``
    matching.  It is excluded from equality for context-insensitive types;
    :attr:`semantic_context` carries the namespace bindings that actually
    affect QName/NOTATION identity.
    """

    library_uri: str
    type_name: str
    xsd_type: type[Any]
    parameters: tuple[DatatypeParameter, ...]
    context: DatatypeContext = field(compare=False)
    #: Derived entirely from ``parameters``; excluded from equality so a
    #: compiled regex object's identity can never affect datatype identity.
    constraints: FacetConstraints = field(compare=False)
    white_space: str
    semantic_context: tuple[tuple[str, str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        semantic = tuple(self.context.namespaces.items()) if self.type_name in _QNAME_TYPES else ()
        object.__setattr__(self, "semantic_context", semantic)

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        try:
            self._checked_value(lexical, context)
        except (TypeError, ValueError):
            return False
        return True

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        """Compare two values in this datatype's value space.

        Facet parameters do not participate: RELAX NG's ``datatypeEqual``
        service has no parameters.  A lexical form outside the base datatype
        has no value and therefore compares unequal to everything.
        """
        left_value = self._equality_value(left, left_context)
        if left_value is None:
            return False
        right_value = self._equality_value(right, right_context)
        if right_value is None:
            return False
        return self._equality_key(left_value) == self._equality_key(right_value)

    def decode(self, lexical: str, context: DatatypeContext) -> Any:
        """Decode a validated lexical form into its Python value.

        ``hexBinary`` and ``base64Binary`` return ``bytes``; ``boolean``
        returns a Python ``bool``; QName and NOTATION return a
        context-resolved :class:`~pyxsd.xsd_data_types.QName`; every other
        type returns its XSD value object (an ``int``, ``Decimal``, ``float``,
        or ``str`` subclass).  Facets are enforced, so an out-of-range or
        pattern-violating lexical form raises ``TypeError`` or ``ValueError``
        instead of producing a value outside the datatype.
        """
        _, value = self._checked_value(lexical, context)
        if isinstance(value, Boolean):
            return bool(int(value))
        if isinstance(value, (Base64Binary, HexBinary)):
            return value.to_bytes()
        return value

    def encode(self, value: Any, context: DatatypeContext) -> str:
        """Encode a Python value as a canonical lexical form.

        Expected inputs are ``bytes``-like for binary types, ``bool`` or
        ``int`` for boolean, ``int`` for the integer family, ``Decimal`` or
        ``int`` for decimal, ``int`` or ``float`` for float/double, ``str`` for
        string and temporal types, and ``QName`` or ``str`` for QName and
        NOTATION.  The produced lexical form is validated against the
        datatype's facets.  A QName prefix must be bound in *context*; a
        resolved QName must denote the same namespace there, so no unresolved
        prefix is invented.
        """
        lexical = self._encode_lexical(value, context)
        self._checked_value(lexical, context)
        return lexical

    def _prepare(self, lexical: str, context: DatatypeContext) -> tuple[str, Any]:
        """Apply whitespace processing and build the XSD value object."""
        text = whitespace_transform(self.white_space, lexical)
        if self.type_name in _YEAR_ZERO_TYPES and _XSD11_YEAR_ZERO.match(text):
            raise TypeError(
                f"xs:{self.type_name} lexical {text!r} uses year 0000, which XSD 1.0 does not allow"
            )
        return text, self._build_value(text, context)

    def _checked_value(self, lexical: str, context: DatatypeContext) -> tuple[str, Any]:
        text, value = self._prepare(lexical, context)
        self.constraints.check(value, text)
        return text, value

    def _equality_value(self, lexical: str, context: DatatypeContext) -> Any | None:
        """Build a value for equality, ignoring facet parameters.

        RELAX NG's ``datatypeEqual`` service has no parameter argument, so a
        lexical form that fails a local facet restriction still has a value.
        A lexical form outside the base datatype has no value and compares
        unequal to everything.
        """
        try:
            _, value = self._prepare(lexical, context)
        except (TypeError, ValueError):
            return None
        return value

    def _build_value(self, text: str, context: DatatypeContext) -> Any:
        if self.type_name in _QNAME_TYPES:
            return self._build_qname(text, context)
        return self.xsd_type(text)

    def _build_qname(self, text: str, context: DatatypeContext) -> QName:
        bindings = _namespace_bindings(context)
        with qname_context(bindings):
            value = QName(text)
        prefix = value._prefix_
        if prefix and not bindings.get(prefix):
            raise TypeError(f"QName prefix {prefix!r} is not bound in the datatype context")
        return value

    def _equality_key(self, value: Any) -> tuple[Any, ...]:
        if isinstance(value, QName):
            return ("QName", (value._uri_ or "", value._local_))
        if isinstance(value, Duration):
            return ("duration", _xsd10_duration_key(str(value)))
        return xsd_value_key(value)

    def _encode_lexical(self, value: Any, context: DatatypeContext) -> str:
        if self.type_name in _QNAME_TYPES:
            return self._encode_qname(value, context)
        if issubclass(self.xsd_type, (Base64Binary, HexBinary)):
            return self._encode_binary(value)
        if issubclass(self.xsd_type, Boolean):
            if not isinstance(value, (bool, int)):
                raise TypeError(f"expected a bool for xs:{self.type_name}")
            return self.xsd_type(value).lexical()
        if issubclass(self.xsd_type, Decimal):
            if isinstance(value, (bool, Boolean)):
                raise TypeError(f"expected a decimal for xs:{self.type_name}")
            if isinstance(value, decimal.Decimal):
                text = format(value, "f")
            elif isinstance(value, int):
                text = str(value)
            else:
                raise TypeError(f"expected a decimal for xs:{self.type_name}")
            return self.xsd_type(text).lexical()
        if issubclass(self.xsd_type, Integer):
            if isinstance(value, (bool, Boolean)) or not isinstance(value, int):
                raise TypeError(f"expected an integer for xs:{self.type_name}")
            return self.xsd_type(value).lexical()
        if issubclass(self.xsd_type, Double):
            if isinstance(value, (bool, Boolean)):
                raise TypeError(f"expected a number for xs:{self.type_name}")
            if not isinstance(value, (int, float)):
                raise TypeError(f"expected a number for xs:{self.type_name}")
            return self.xsd_type(cast(Any, value)).lexical()
        if not isinstance(value, str):
            raise TypeError(f"expected a string for xs:{self.type_name}")
        return self.xsd_type(value).lexical()

    def _encode_binary(self, value: Any) -> str:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError(f"expected bytes for xs:{self.type_name}")
        raw = bytes(value)
        if issubclass(self.xsd_type, HexBinary):
            return raw.hex().upper()
        return base64.b64encode(raw).decode("ascii")

    def _encode_qname(self, value: Any, context: DatatypeContext) -> str:
        if isinstance(value, QName):
            lexical = str(value)
            if value._resolved_:
                prefix = value._prefix_
                uri = value._uri_
                if prefix:
                    if not uri:
                        raise ValueError(
                            f"QName {lexical!r} has no namespace for prefix {prefix!r}"
                        )
                    if _namespace_bindings(context).get(prefix) != uri:
                        raise ValueError(
                            f"QName prefix {prefix!r} does not denote {uri!r} "
                            f"in the encoding context"
                        )
                else:
                    default = context.namespaces.get("") or None
                    if (uri or None) != default:
                        raise ValueError(
                            f"QName {lexical!r} denotes {uri!r}, but the encoding "
                            f"context has default namespace {default!r}"
                        )
                return lexical
            self._build_qname(lexical, context)
            return lexical
        if isinstance(value, str):
            self._build_qname(value, context)
            return value
        raise TypeError(f"expected a QName for xs:{self.type_name}")


class BuiltinDatatypeFactory(DatatypeFactory):
    """RELAX NG's built-in datatype library: ``string`` and ``token``."""

    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        if parameters:
            raise DatatypeParameterError(
                "the RELAX NG built-in datatype library accepts no parameters"
            )
        xsd_type: type[Any]
        white_space: str
        if type_name == "string":
            xsd_type, white_space = String, "preserve"
        elif type_name == "token":
            xsd_type, white_space = Token, "collapse"
        else:
            raise UnsupportedDatatypeError(
                f"the RELAX NG built-in datatype library has no datatype {type_name!r}"
            )
        return ScalarDatatype(
            library_uri=BUILTIN_LIBRARY,
            type_name=type_name,
            xsd_type=xsd_type,
            parameters=(),
            context=context,
            constraints=FacetConstraints(),
            white_space=white_space,
        )


class _FacetSource:
    """The facet attributes :func:`build_constraints` reads from a representative."""

    xsdElement = None
    patterns: tuple[str, ...] = ()
    enumerations: tuple[str, ...] = ()
    whiteSpace: str | None = None
    explicitTimezone: str | None = None

    def __init__(self, parameters: tuple[DatatypeParameter, ...]) -> None:
        values = {parameter.name: parameter.value for parameter in parameters}
        for name in _FACET_NAMES:
            setattr(self, name, values.get(name))


def _xsd_white_space(xsd_type: type[Any]) -> str:
    """The XSD fixed ``whiteSpace`` value of a built-in type.

    ``xs:string`` and the unconstrained ``xs:anySimpleType`` preserve their
    lexical form; ``xs:normalizedString`` replaces tab/newline/CR with spaces;
    every other built-in collapses XML whitespace.
    """
    name = xsd_type.__dict__.get("name")
    if name in ("string", "anySimpleType"):
        return "preserve"
    if name == "normalizedString":
        return "replace"
    return "collapse"


def _prepare_xsd_parameters(
    type_name: str, parameters: tuple[DatatypeParameter, ...]
) -> tuple[DatatypeParameter, ...]:
    """Validate parameter names and duplicates, then return a canonical order."""
    seen: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, DatatypeParameter):
            raise TypeError("datatype parameters must be DatatypeParameter instances")
        if parameter.name in _REJECTED_XSD_PARAMETERS:
            raise DatatypeParameterError(
                f"the RELAX NG XSD datatype guidelines reject the {parameter.name!r} parameter"
            )
        if parameter.name not in _XSD_PARAMETER_NAMES:
            raise DatatypeParameterError(
                f"unknown XSD datatype parameter {parameter.name!r} for xs:{type_name}"
            )
        if parameter.name in _BOUND_PARAMETER_NAMES and type_name not in _ORDERED_TYPE_NAMES:
            raise DatatypeParameterError(
                f"facet {parameter.name!r} is not applicable to xs:{type_name}"
            )
        if parameter.name != "pattern":
            if parameter.name in seen:
                raise DatatypeParameterError(
                    f"duplicate {parameter.name!r} parameter for xs:{type_name}"
                )
            seen.add(parameter.name)
    return tuple(sorted(parameters, key=lambda item: (item.name, item.value)))


def _unsupported_xsd_type(type_name: str) -> UnsupportedDatatypeError:
    """Return the explicit capability failure for an unavailable XSD type."""
    if type_name in _XSD_11_ONLY_TYPE_NAMES:
        return UnsupportedDatatypeError(
            f"xsd:{type_name} is an XSD 1.1 datatype and is not available "
            f"in the XSD 1.0 datatype library"
        )
    if type_name == "anyType":
        return UnsupportedDatatypeError(
            "xsd:anyType is not a simple datatype in the XSD 1.0 datatype library"
        )
    return UnsupportedDatatypeError(f"unknown XSD datatype {type_name!r}")


class XsdDatatypeFactory(DatatypeFactory):
    """The RELAX NG XSD datatype library with XSD 1.0 semantics."""

    def create(
        self,
        type_name: str,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        xsd_type = _XSD_10_TYPES.get(type_name)
        if xsd_type is None:
            raise _unsupported_xsd_type(type_name)
        prepared = _prepare_xsd_parameters(type_name, parameters)
        result = build_constraints(
            _FacetSource(prepared),
            xsd_type,
            None,
            base_factory=xsd_type,
            xsd_version="1.0",
        )
        problems = (*result.errors, *result.conflicts)
        if problems:
            raise DatatypeParameterError(
                f"invalid parameters for xs:{type_name}: " + "; ".join(problems)
            )
        constraints = result.constraints
        patterns = tuple(
            pattern
            for parameter in prepared
            if parameter.name == "pattern"
            for pattern in (self._compile_pattern(parameter.value),)
        )
        if patterns:
            constraints = replace(constraints, patterns=tuple((pattern,) for pattern in patterns))
        return ScalarDatatype(
            library_uri=XSD_LIBRARY,
            type_name=type_name,
            xsd_type=xsd_type,
            parameters=prepared,
            context=context,
            constraints=constraints,
            white_space=_xsd_white_space(xsd_type),
        )

    @staticmethod
    def _compile_pattern(text: str) -> re.Pattern[str]:
        try:
            return compile_pattern(text, xsd_version="1.0")
        except ValueError as exc:
            raise DatatypeParameterError(str(exc)) from exc


def _namespace_bindings(context: DatatypeContext) -> dict[str, str]:
    """The effective prefix map, including the implicitly bound ``xml`` prefix."""
    bindings = dict(context.namespaces)
    bindings.setdefault("xml", _XML_NAMESPACE)
    return bindings


def _xsd10_duration_key(text: str) -> tuple[int, int, int, int, int, int, decimal.Decimal]:
    """The XSD 1.0 six-dimensional identity key for ``xs:duration``.

    XSD 1.0 treats the year, month, day, hour, minute and second components as
    independent coordinates, so ``P1Y`` and ``P12M`` are distinct values.
    XSD 1.1 changed this; the RELAX NG XSD adapter deliberately keeps 1.0.
    """
    match = _DURATION_PARTS.fullmatch(text)
    assert match is not None
    groups = match.groups()
    years, months, days, hours, minutes, seconds_text = groups
    components = [
        int(component[:-1]) if component is not None else 0
        for component in (years, months, days, hours, minutes)
    ]
    seconds = decimal.Decimal(seconds_text[:-1]) if seconds_text is not None else decimal.Decimal(0)
    zero = not any(components) and not seconds
    sign = 1 if zero or not text.startswith("-") else -1
    key: tuple[int, int, int, int, int, int, decimal.Decimal] = (
        sign,
        components[0],
        components[1],
        components[2],
        components[3],
        components[4],
        seconds,
    )
    return key


def resolve_datatype_selector(selector: DatatypeSelector) -> tuple[str, str]:
    """Resolve a selector to ``(library_uri, type_name)``.

    A bare string names a type in the built-in library.  The documented
    ``xsd:`` shorthand names a type in the XSD library; it is a fixed alias,
    never a prefix lookup against the datatype context.  A tuple is an
    explicit ``(library_uri, type_name)`` selector.
    """
    if isinstance(selector, str):
        if selector.startswith(_XSD_SHORTHAND):
            type_name = selector[len(_XSD_SHORTHAND) :]
            if not type_name:
                raise UnsupportedDatatypeError("the 'xsd:' datatype selector requires a type name")
            return (XSD_LIBRARY, type_name)
        if ":" in selector:
            raise UnsupportedDatatypeError(
                f"unsupported datatype selector {selector!r}: use the 'xsd:' "
                f"shorthand or a (library_uri, type_name) tuple"
            )
        return (BUILTIN_LIBRARY, selector)
    if isinstance(selector, tuple) and len(selector) == 2:
        library_uri, type_name = selector
        if not isinstance(library_uri, str) or not isinstance(type_name, str):
            raise TypeError("a datatype selector tuple must contain two strings")
        return (library_uri, type_name)
    raise TypeError("a datatype selector must be a string or a (library_uri, type_name) tuple")


@dataclass(frozen=True)
class DatatypeLibraryRegistration:
    """A custom datatype library factory and its stable configuration identity."""

    factory: DatatypeFactory
    fingerprint: str


class DatatypeLibraryRegistry:
    """The available datatype libraries with fixed built-in/XSD identities."""

    def __init__(self) -> None:
        self._registrations: dict[str, DatatypeLibraryRegistration] = {
            BUILTIN_LIBRARY: DatatypeLibraryRegistration(
                BuiltinDatatypeFactory(), BUILTIN_FINGERPRINT
            ),
            XSD_LIBRARY: DatatypeLibraryRegistration(XsdDatatypeFactory(), XSD_FINGERPRINT),
        }

    def library_uris(self) -> tuple[str, ...]:
        """The registered library URIs in deterministic order."""
        return tuple(sorted(self._registrations))

    def registration(self, uri: str) -> DatatypeLibraryRegistration:
        """Return the registration for *uri* or raise an unsupported failure."""
        try:
            return self._registrations[uri]
        except KeyError:
            raise UnsupportedDatatypeError(
                f"unsupported RELAX NG datatype library {uri!r}"
            ) from None

    def register(self, uri: str, registration: DatatypeLibraryRegistration) -> None:
        """Register a custom datatype library under *uri*.

        The built-in and XSD identities are fixed and cannot be replaced.
        Every custom fingerprint must be stable and process-independent, so a
        blank string or a captured object address is rejected.
        """
        if not isinstance(uri, str):
            raise TypeError("a datatype library URI must be a string")
        if not isinstance(registration, DatatypeLibraryRegistration):
            raise TypeError("expected a DatatypeLibraryRegistration")
        if uri in self._registrations:
            raise DatatypeRegistrationError(f"a datatype library is already registered for {uri!r}")
        _validate_fingerprint(registration.fingerprint)
        if not isinstance(registration.factory, DatatypeFactory) and not callable(
            getattr(registration.factory, "create", None)
        ):
            raise DatatypeRegistrationError(
                "a datatype library factory must provide a create() method"
            )
        self._registrations[uri] = registration

    def resolve(self, selector: DatatypeSelector) -> tuple[str, str]:
        """Resolve a selector without constructing a datatype."""
        return resolve_datatype_selector(selector)

    def create(
        self,
        selector: DatatypeSelector,
        parameters: tuple[DatatypeParameter, ...],
        context: DatatypeContext,
    ) -> Datatype:
        """Compile the selected datatype."""
        library_uri, type_name = self.resolve(selector)
        return self.registration(library_uri).factory.create(type_name, parameters, context)


def _validate_fingerprint(fingerprint: str) -> None:
    """Reject a blank or process-dependent library fingerprint."""
    if not isinstance(fingerprint, str):
        raise DatatypeRegistrationError("a custom datatype library fingerprint must be a string")
    if not fingerprint.strip():
        raise DatatypeRegistrationError("a custom datatype library fingerprint must not be blank")
    if _PROCESS_DEPENDENT_FINGERPRINT.search(fingerprint):
        raise DatatypeRegistrationError(
            "a custom datatype library fingerprint must not depend on a process address"
        )
