"""XSLT 3.0 named components, declaration binding, and visibility.

This module owns the declaration side of package compilation: it validates and
groups named declarations, constructs their signatures, applies ``xsl:expose``
and ``xsl:accept`` visibility rules, builds overrides, and binds symbolic
references to the completed component table.  Package composition in
:mod:`pyxsd.xslt.compiler.packages` consumes these helpers; there is no second
component graph here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from pyxsd.locations import IssueLocation
from pyxsd.namespaces import local_name as _local_name
from pyxsd.namespaces import namespace_of as _namespace_of
from pyxsd.xslt.compiler.attribute_values import require_item_type
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.expressions import CompiledSequenceType, compile_sequence_type
from pyxsd.xslt.compiler.instructions import (
    ComponentReference,
    Instruction,
    InstructionNode,
    SequenceConstructor,
    TextNode,
    normalize_element,
)
from pyxsd.xslt.compiler.static import EffectiveElement, EffectiveModule
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.precedence import compare_precedence

if TYPE_CHECKING:
    from pyxsd.xdm.schema import SchemaPool
    from pyxsd.xslt.compiler.packages import CompiledPackage, PackageDescriptor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
_XSLT = f"{{{XSLT_NS}}}"

_TEMPLATE = f"{_XSLT}template"
_FUNCTION = f"{_XSLT}function"
_VARIABLE = f"{_XSLT}variable"
_PARAM = f"{_XSLT}param"
_ATTRIBUTE_SET = f"{_XSLT}attribute-set"
_MODE = f"{_XSLT}mode"
_ACCUMULATOR = f"{_XSLT}accumulator"
_KEY = f"{_XSLT}key"
_OUTPUT = f"{_XSLT}output"
_DECIMAL_FORMAT = f"{_XSLT}decimal-format"
_CHARACTER_MAP = f"{_XSLT}character-map"
_USE_PACKAGE = f"{_XSLT}use-package"
_EXPOSE = f"{_XSLT}expose"
_ACCEPT = f"{_XSLT}accept"
_OVERRIDE = f"{_XSLT}override"
_STYLESHEET = f"{_XSLT}stylesheet"
_TRANSFORM = f"{_XSLT}transform"
_APPLY_TEMPLATES = f"{_XSLT}apply-templates"
_CONTEXT_ITEM = f"{_XSLT}context-item"

_COMPONENT_KINDS = frozenset({"template", "function", "variable", "attribute-set", "mode"})
_DECLARATION_VISIBILITIES = frozenset({"public", "private", "final", "abstract"})
_ACCEPT_VISIBILITIES = frozenset({"public", "private", "final", "abstract", "hidden"})
_XPATH_REFERENCE_ATTRIBUTES = frozenset(
    {
        "select",
        "test",
        "use",
        "initial-value",
        "group-by",
        "group-adjacent",
        "group-starting-with",
        "for-each",
        "from",
        "count",
        "value",
        "match",
        "regex",
        "fields",
        "names",
        "use-when",
    }
)

ComponentKey: TypeAlias = tuple[str, str | None, int | None]


def _component_key(kind: str, name: str | None, arity: int | None = None) -> ComponentKey:
    return (kind, name, arity if kind == "function" else None)


def _error(
    message: str,
    code: str,
    *,
    element: EffectiveElement | None = None,
    module: EffectiveModule | None = None,
    uri: str | None = None,
) -> XsltStaticError:
    location = None if element is None else element.source_location()
    if uri is None and module is not None:
        uri = module.identity
    return XsltStaticError(message, code=code, uri=uri, location=location)


#: Namespaces XSLT 3.0 section 3.2 reserves from stylesheet-defined names.
_RESERVED_NAMESPACES = frozenset(
    {
        XSLT_NS,
        "http://www.w3.org/2005/xpath-functions",
        "http://www.w3.org/2005/xpath-functions/math",
        "http://www.w3.org/2005/xpath-functions/map",
        "http://www.w3.org/2005/xpath-functions/array",
        "http://www.w3.org/2001/XMLSchema",
        "http://www.w3.org/2001/XMLSchema-instance",
        "http://www.w3.org/XML/1998/namespace",
        "http://www.w3.org/2000/xmlns/",
        "http://www.w3.org/2005/xqt-errors",
    }
)

#: The one reserved name the specification permits as a template name.
_INITIAL_TEMPLATE = f"{_XSLT}initial-template"


def _check_reserved_name(
    expanded: str,
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    allow_initial_template: bool = False,
) -> None:
    """Reject a stylesheet-defined name in a reserved namespace (XTSE0080).

    The namespaces in :data:`_RESERVED_NAMESPACES` are reserved for the
    specification; ``xsl:initial-template`` is the one documented exception.
    """
    namespace = _namespace_of(expanded) or ""
    if namespace not in _RESERVED_NAMESPACES:
        return
    if allow_initial_template and expanded == _INITIAL_TEMPLATE:
        return
    raise _error(
        "a reserved namespace must not be used in the name of a stylesheet-defined object",
        "XTSE0080",
        element=element,
        module=module,
    )


def _expand_name(
    value: str,
    namespaces: Mapping[str, str],
    *,
    element: EffectiveElement | None = None,
    module: EffectiveModule | None = None,
    uri: str | None = None,
    code: str = "XTSE0280",
) -> str:
    name = value.strip()
    if not name:
        raise _error(
            "an expanded name must not be empty",
            "XTSE0010",
            element=element,
            module=module,
            uri=uri,
        )
    if name.startswith("Q{"):
        close = name.find("}")
        if close < 0 or close + 1 >= len(name):
            raise _error(
                f"the expanded name {name!r} is not a valid EQName",
                "XTSE0010",
                element=element,
                module=module,
                uri=uri,
            )
        uri_value = name[2:close]
        local = name[close + 1 :]
        return local if not uri_value else f"{{{uri_value}}}{local}"
    if name.startswith("{"):
        return name
    prefix, separator, local = name.partition(":")
    if not separator:
        return name
    bound_uri = namespaces.get(prefix)
    if bound_uri is None:
        raise _error(
            f"the namespace prefix {prefix!r} is not bound in this static context",
            code,
            element=element,
            module=module,
            uri=uri,
        )
    return f"{{{bound_uri}}}{local}"


def _yes_no(
    value: str | None,
    default: bool,
    *,
    element: EffectiveElement,
    module: EffectiveModule,
    attribute: str,
) -> bool:
    if value is None:
        return default
    normalized = value.strip()
    if normalized in ("yes", "true", "1"):
        return True
    if normalized in ("no", "false", "0"):
        return False
    raise _error(
        f"the {attribute} attribute has an invalid value {value!r}",
        "XTSE0020",
        element=element,
        module=module,
    )


@dataclass(frozen=True, slots=True)
class ParameterSignature:
    """One parameter's name, type, and invocation flags."""

    name: str
    sequence_type: CompiledSequenceType
    required: bool = False
    tunnel: bool = False


@dataclass(frozen=True, slots=True)
class ContextItemSignature:
    """One template's ``xsl:context-item`` declaration, normalized."""

    use: str
    sequence_type: CompiledSequenceType


@dataclass(frozen=True, slots=True)
class Signature:
    """A component signature used for override compatibility checks."""

    kind: str
    return_type: CompiledSequenceType | None = None
    parameters: tuple[ParameterSignature, ...] = ()
    context_item: ContextItemSignature | None = None
    sequence_type: CompiledSequenceType | None = None
    required: bool = False
    variable_kind: str = "variable"
    flags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "flags", MappingProxyType(dict(self.flags)))


@dataclass(frozen=True, slots=True)
class Component:
    """One immutable named component in its containing package.

    ``owner`` is the declaring package; ``containing`` is the package whose
    component table this copy belongs to.  Accepted copies keep the original
    declaration and body, while an overriding declaration has the using
    package as both owner and containing package.
    """

    kind: str
    body: SequenceConstructor
    declaration: Instruction
    name: str | None = None
    arity: int | None = None
    visibility: str = "private"
    declared_visibility: str | None = None
    owner: str | None = None
    containing: str | None = None
    module: str | None = None
    precedence: tuple[int, ...] = ()
    declaration_order: int = 0
    declarations: tuple[Instruction, ...] = ()
    signature: Signature | None = None
    location: IssueLocation | None = None
    original_abstract: bool = False

    @property
    def hidden(self) -> bool:
        return self.visibility == "hidden"

    @property
    def abstract(self) -> bool:
        return self.visibility == "abstract"


@dataclass(frozen=True, slots=True)
class _Declaration:
    kind: str
    element: EffectiveElement
    module: EffectiveModule
    name: str | None
    arity: int | None
    order: int
    precedence: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _AcceptRule:
    element: EffectiveElement
    module: EffectiveModule
    kinds: frozenset[str]
    visibility: str
    tokens: tuple[_NameToken, ...]
    order: int


@dataclass(frozen=True, slots=True)
class _ExposeRule:
    element: EffectiveElement
    module: EffectiveModule
    kinds: frozenset[str]
    visibility: str
    tokens: tuple[_NameToken, ...]
    order: int


@dataclass(frozen=True, slots=True)
class _NameToken:
    kind: Literal["all", "namespace", "local", "exact", "function"]
    name: str | None = None
    namespace: str | None = None
    local: str | None = None
    arity: int | None = None

    @property
    def specific(self) -> bool:
        return self.kind in ("exact", "function")

    def matches(self, kind: str, name: str | None, arity: int | None) -> bool:
        if name is None:
            return False
        if self.kind == "all":
            return True
        namespace = _namespace_of(name) or ""
        local = _local_name(name)
        if self.kind == "namespace":
            return namespace == (self.namespace or "")
        if self.kind == "local":
            return local == self.local
        if self.kind == "exact":
            return name == self.name
        if kind != "function":
            return False
        return name == self.name and arity == self.arity


def _matching_tokens(
    rules: Iterable[_ExposeRule | _AcceptRule],
    kind: str,
    name: str | None,
    arity: int | None,
) -> list[tuple[_ExposeRule | _AcceptRule, int, _NameToken]]:
    matches: list[tuple[_ExposeRule | _AcceptRule, int, _NameToken]] = []
    for rule in rules:
        if kind not in rule.kinds:
            continue
        for index, token in enumerate(rule.tokens):
            if token.matches(kind, name, arity):
                matches.append((rule, index, token))
    return matches


def _ordered_matches(
    matches: Iterable[tuple[_ExposeRule | _AcceptRule, int, _NameToken]],
) -> list[tuple[_ExposeRule | _AcceptRule, int, _NameToken]]:
    def rank(item: tuple[_ExposeRule | _AcceptRule, int, _NameToken]) -> tuple[int, int, int]:
        rule, index, token = item
        specificity = 3 if token.specific else 2 if token.kind in ("namespace", "local") else 1
        return specificity, rule.order, index

    return sorted(matches, key=rank, reverse=True)


@dataclass(frozen=True, slots=True)
class _GroupedDeclaration:
    kind: str
    name: str | None
    arity: int | None
    declarations: tuple[_Declaration, ...]
    precedence: tuple[int, ...]
    declared_visibility: str | None
    default_visibility: str
    order: int


def _visibility(
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    mode: bool = False,
) -> str | None:
    value = element.attribute("visibility")
    if value is None:
        return None
    allowed = frozenset({"public", "private", "final"}) if mode else _DECLARATION_VISIBILITIES
    if value not in allowed:
        raise _error(
            f"invalid visibility value {value!r}",
            "XTSE0020",
            element=element,
            module=module,
        )
    return value


def _parse_declaration(
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    order: int,
) -> list[_Declaration]:
    if element.name == _TEMPLATE:
        name_value = element.attribute("name")
        match = element.attribute("match")
        if not name_value and not match:
            raise _error(
                "an xsl:template element requires a match or name attribute",
                "XTSE0500",
                element=element,
                module=module,
            )
        _visibility(element, module)
        if not match and (
            element.attribute("mode") is not None or element.attribute("priority") is not None
        ):
            raise _error(
                "a template without a match attribute must not have mode or priority",
                "XTSE0500",
                element=element,
                module=module,
            )
        assert name_value is not None
        if not name_value.strip():
            raise _error(
                "a named template requires a non-empty name",
                "XTSE0010",
                element=element,
                module=module,
            )
        expanded = _expand_name(
            name_value, element.in_scope_namespaces, element=element, module=module
        )
        _check_reserved_name(expanded, element, module, allow_initial_template=True)
        return [
            _Declaration(
                kind="template",
                element=element,
                module=module,
                name=expanded,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name == _FUNCTION:
        name_value = element.attribute("name")
        if not name_value:
            raise _error(
                "an xsl:function requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        expanded = _expand_name(
            name_value, element.in_scope_namespaces, element=element, module=module
        )
        if not expanded.startswith("{"):
            raise _error(
                "a stylesheet function name must be in a namespace",
                "XTSE0740",
                element=element,
                module=module,
            )
        _check_reserved_name(expanded, element, module)
        _visibility(element, module)
        arity = sum(1 for child in element.elements if child.name == _PARAM)
        return [
            _Declaration(
                kind="function",
                element=element,
                module=module,
                name=expanded,
                arity=arity,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name in (_VARIABLE, _PARAM):
        name_value = element.attribute("name")
        if not name_value:
            raise _error(
                "a global variable or parameter requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        expanded = _expand_name(
            name_value, element.in_scope_namespaces, element=element, module=module
        )
        if element.name == _PARAM:
            if element.attribute("visibility") is not None:
                raise _error(
                    "xsl:param does not accept a visibility attribute",
                    "XTSE0090",
                    element=element,
                    module=module,
                )
        else:
            _visibility(element, module)
        _check_reserved_name(expanded, element, module)
        return [
            _Declaration(
                kind="variable",
                element=element,
                module=module,
                name=expanded,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name == _ATTRIBUTE_SET:
        name_value = element.attribute("name")
        if not name_value:
            raise _error(
                "an xsl:attribute-set requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        expanded = _expand_name(
            name_value, element.in_scope_namespaces, element=element, module=module
        )
        _visibility(element, module)
        _check_reserved_name(expanded, element, module)
        return [
            _Declaration(
                kind="attribute-set",
                element=element,
                module=module,
                name=expanded,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name == _MODE:
        name_value = element.attribute("name")
        if name_value == "#unnamed":
            raise _error(
                "an xsl:mode declaration cannot name #unnamed",
                "XTSE0020",
                element=element,
                module=module,
            )
        mode_name: str | None = (
            None
            if name_value is None
            else _expand_name(
                name_value,
                element.in_scope_namespaces,
                element=element,
                module=module,
            )
        )
        if mode_name is not None:
            _check_reserved_name(mode_name, element, module)
        declared_visibility = _visibility(element, module, mode=True)
        if mode_name is None and declared_visibility not in (None, "private"):
            raise _error(
                "the unnamed mode must be private",
                "XTSE0020",
                element=element,
                module=module,
            )
        return [
            _Declaration(
                kind="mode",
                element=element,
                module=module,
                name=mode_name,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name == _ACCUMULATOR:
        name_value = element.attribute("name")
        if not name_value:
            raise _error(
                "an xsl:accumulator requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        expanded = _expand_name(
            name_value, element.in_scope_namespaces, element=element, module=module
        )
        if element.attribute("visibility") is not None:
            raise _error(
                "xsl:accumulator does not accept a visibility attribute",
                "XTSE0090",
                element=element,
                module=module,
            )
        _check_reserved_name(expanded, element, module)
        return [
            _Declaration(
                kind="accumulator",
                element=element,
                module=module,
                name=expanded,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name == _KEY:
        name_value = element.attribute("name")
        if not name_value:
            raise _error(
                "an xsl:key requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        if element.attribute("visibility") is not None:
            raise _error(
                "xsl:key does not accept a visibility attribute",
                "XTSE0020",
                element=element,
                module=module,
            )
        if element.attribute("match") is None or element.attribute("use") is None:
            raise _error(
                "xsl:key requires match and use attributes",
                "XTSE0010",
                element=element,
                module=module,
            )
        expanded = _expand_name(
            name_value, element.in_scope_namespaces, element=element, module=module
        )
        _check_reserved_name(expanded, element, module)
        return [
            _Declaration(
                kind="key",
                element=element,
                module=module,
                name=expanded,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    if element.name in (_OUTPUT, _DECIMAL_FORMAT, _CHARACTER_MAP):
        name_value = element.attribute("name")
        if element.name == _CHARACTER_MAP and not name_value:
            raise _error(
                "xsl:character-map requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        construct_name: str | None = None
        if name_value:
            construct_name = _expand_name(
                name_value, element.in_scope_namespaces, element=element, module=module
            )
            _check_reserved_name(construct_name, element, module)
        kind = {
            _OUTPUT: "output",
            _DECIMAL_FORMAT: "decimal-format",
            _CHARACTER_MAP: "character-map",
        }[element.name]
        return [
            _Declaration(
                kind=kind,
                element=element,
                module=module,
                name=construct_name,
                arity=None,
                order=order,
                precedence=module.precedence,
            )
        ]

    return []


def _parse_name_token(
    token: str,
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    code: str,
) -> _NameToken:
    if token == "*":
        return _NameToken("all")
    if token.startswith("#"):
        raise _error(
            f"the special mode name {token!r} is not valid in this component selector",
            "XTSE0020",
            element=element,
            module=module,
        )
    arity: int | None = None
    name = token
    if token.startswith("Q{"):
        close = token.find("}")
        if close < 0:
            raise _error(f"invalid EQName {token!r}", code, element=element, module=module)
        uri = token[2:close]
        rest = token[close + 1 :]
        if rest == "*":
            return _NameToken("namespace", namespace=uri)
        if "#" in rest:
            local, _, arity_text = rest.partition("#")
            arity = _parse_arity(arity_text, element, module, code)
        else:
            local = rest
        if not local:
            raise _error(f"invalid EQName {token!r}", code, element=element, module=module)
        expanded = local if not uri else f"{{{uri}}}{local}"
    else:
        if "#" in token:
            name, _, arity_text = token.partition("#")
            arity = _parse_arity(arity_text, element, module, code)
        if name.startswith("*:"):
            if arity is not None:
                raise _error(
                    f"a wildcard name {name!r} cannot carry a function arity",
                    code,
                    element=element,
                    module=module,
                )
            return _NameToken("local", local=name[2:])
        if name.endswith(":*"):
            prefix = name[:-2]
            prefix_uri = element.in_scope_namespaces.get(prefix)
            if prefix_uri is None:
                raise _error(
                    f"the namespace prefix {prefix!r} is not bound",
                    "XTSE0020",
                    element=element,
                    module=module,
                )
            if arity is not None:
                raise _error(
                    f"a wildcard name {name!r} cannot carry a function arity",
                    code,
                    element=element,
                    module=module,
                )
            return _NameToken("namespace", namespace=prefix_uri)
        expanded = _expand_name(
            name,
            element.in_scope_namespaces,
            element=element,
            module=module,
            code="XTSE0020",
        )
    if arity is not None:
        return _NameToken("function", name=expanded, arity=arity)
    return _NameToken("exact", name=expanded)


def _parse_arity(
    arity_text: str,
    element: EffectiveElement,
    module: EffectiveModule,
    code: str,
) -> int:
    try:
        arity = int(arity_text)
    except ValueError as exc:
        raise _error(
            f"invalid function arity {arity_text!r}",
            code,
            element=element,
            module=module,
        ) from exc
    if arity < 0:
        raise _error("a function arity must be non-negative", code, element=element, module=module)
    return arity


def _parse_selector(
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    accept: bool,
    order: int,
) -> _ExposeRule | _AcceptRule:
    component = (element.attribute("component") or "").strip()
    if component == "*":
        kinds = _COMPONENT_KINDS
    elif component in _COMPONENT_KINDS:
        kinds = frozenset({component})
    else:
        raise _error(
            f"unknown component kind {component!r}",
            "XTSE0020",
            element=element,
            module=module,
        )
    visibility = element.attribute("visibility")
    allowed = (
        _ACCEPT_VISIBILITIES if accept else frozenset({"public", "private", "final", "abstract"})
    )
    if visibility not in allowed or (accept and "mode" in kinds and visibility == "abstract"):
        raise _error(
            f"invalid visibility value {visibility!r}",
            "XTSE0020",
            element=element,
            module=module,
        )
    names = element.attribute("names")
    if names is None or not names.split():
        raise _error(
            "a component selector requires names",
            "XTSE0010",
            element=element,
            module=module,
        )
    code = "XTSE3030" if accept else "XTSE3020"
    tokens = tuple(_parse_name_token(token, element, module, code=code) for token in names.split())
    if component == "*" and any(token.specific for token in tokens):
        raise _error(
            "component='*' can only be used with wildcard names",
            "XTSE3032" if accept else "XTSE3022",
            element=element,
            module=module,
        )
    if accept:
        return _AcceptRule(
            element=element,
            module=module,
            kinds=frozenset(kinds),
            visibility=visibility,
            tokens=tokens,
            order=order,
        )
    return _ExposeRule(
        element=element,
        module=module,
        kinds=frozenset(kinds),
        visibility=visibility,
        tokens=tokens,
        order=order,
    )


def _parse_expose(element: EffectiveElement, module: EffectiveModule, *, order: int) -> _ExposeRule:
    selector = _parse_selector(element, module, accept=False, order=order)
    assert isinstance(selector, _ExposeRule)
    return selector


def _parse_accept(element: EffectiveElement, module: EffectiveModule, *, order: int) -> _AcceptRule:
    selector = _parse_selector(element, module, accept=True, order=order)
    assert isinstance(selector, _AcceptRule)
    return selector


def _parse_priority(element: EffectiveElement, module: EffectiveModule) -> Decimal | None:
    value = element.attribute("priority")
    if value is None:
        return None
    try:
        priority = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise _error(
            f"invalid template priority {value!r}",
            "XTSE0530",
            element=element,
            module=module,
        ) from exc
    if not priority.is_finite():
        raise _error(
            "a template priority must be finite",
            "XTSE0530",
            element=element,
            module=module,
        )
    return priority


def _module_context(module: EffectiveModule, *, schema: SchemaPool | None = None) -> StaticContext:
    return StaticContext.for_module(module, schema=schema)


def _type(
    element: EffectiveElement,
    attribute: str,
    context: StaticContext,
    *,
    default: str,
) -> CompiledSequenceType:
    value = element.attribute(attribute)
    source = default if value is None else value
    return compile_sequence_type(source, context, location=element.source_location())


def _parameter_signatures(
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    schema: SchemaPool | None = None,
) -> tuple[ParameterSignature, ...]:
    context = _module_context(module, schema=schema)
    parameters: list[ParameterSignature] = []
    seen: set[str] = set()
    for child in element.elements:
        if child.name != _PARAM:
            continue
        name_value = child.attribute("name")
        if not name_value:
            raise _error("an xsl:param requires a name", "XTSE0010", element=child, module=module)
        expanded = _expand_name(name_value, child.in_scope_namespaces, element=child, module=module)
        if expanded in seen:
            raise _error(
                f"the parameter {name_value!r} is declared more than once",
                "XTSE0580",
                element=child,
                module=module,
            )
        seen.add(expanded)
        parameters.append(
            ParameterSignature(
                name=expanded,
                sequence_type=_type(child, "as", context, default="item()*"),
                required=_yes_no(
                    child.attribute("required"),
                    False,
                    element=child,
                    module=module,
                    attribute="required",
                ),
                tunnel=_yes_no(
                    child.attribute("tunnel"),
                    False,
                    element=child,
                    module=module,
                    attribute="tunnel",
                ),
            )
        )
    return tuple(parameters)


def _context_item_signature(
    element: EffectiveElement,
    module: EffectiveModule,
    context: StaticContext,
) -> ContextItemSignature:
    child = next((item for item in element.elements if item.name == _CONTEXT_ITEM), None)
    if child is None:
        return ContextItemSignature("optional", compile_sequence_type("item()", context))
    use = child.attribute("use") or "optional"
    if use not in ("required", "optional", "absent"):
        raise _error(
            f"invalid context-item use {use!r}",
            "XTSE0020",
            element=child,
            module=module,
        )
    if use == "absent" and child.attribute("as") is not None:
        raise _error(
            "xsl:context-item with use='absent' must not have an as attribute",
            "XTSE3088",
            element=child,
            module=module,
        )
    sequence_type = _type(child, "as", context, default="item()")
    require_item_type(
        sequence_type,
        label="xsl:context-item/@as",
        uri=module.identity,
        location=child.source_location(),
    )
    return ContextItemSignature(use, sequence_type)


def _signature(declaration: _Declaration, *, schema: SchemaPool | None = None) -> Signature:
    element = declaration.element
    module = declaration.module
    if declaration.kind == "template":
        context = _module_context(module, schema=schema)
        return Signature(
            kind="template",
            return_type=_type(element, "as", context, default="item()*"),
            parameters=_parameter_signatures(element, module, schema=schema),
            context_item=_context_item_signature(element, module, context),
        )
    if declaration.kind == "function":
        context = _module_context(module, schema=schema)
        flags = {
            name: value
            for name in (
                "identity-sensitive",
                "new-each-time",
                "override-extension-functions",
                "streamable",
                "posture",
                "sweep",
            )
            if (value := element.attribute(name)) is not None
        }
        return Signature(
            kind="function",
            return_type=_type(element, "as", context, default="item()*"),
            parameters=_parameter_signatures(element, module, schema=schema),
            flags=flags,
        )
    if declaration.kind == "variable":
        context = _module_context(module, schema=schema)
        required = _yes_no(
            element.attribute("required"),
            False,
            element=element,
            module=module,
            attribute="required",
        )
        return Signature(
            kind="variable",
            sequence_type=_type(element, "as", context, default="item()*"),
            required=required,
            variable_kind="param" if element.name == _PARAM else "variable",
        )
    if declaration.kind == "attribute-set":
        return Signature(
            kind="attribute-set",
            flags={
                "streamable": "yes"
                if _yes_no(
                    element.attribute("streamable"),
                    False,
                    element=element,
                    module=module,
                    attribute="streamable",
                )
                else "no"
            },
        )
    if declaration.kind == "accumulator":
        context = _module_context(module, schema=schema)
        return Signature(
            kind="accumulator",
            sequence_type=_type(element, "as", context, default="item()*"),
            flags={
                "streamable": "yes"
                if _yes_no(
                    element.attribute("streamable"),
                    False,
                    element=element,
                    module=module,
                    attribute="streamable",
                )
                else "no"
            },
        )
    return Signature(kind="key")


def _type_key(value: CompiledSequenceType | None) -> Any:
    assert value is not None
    item = value.item_type
    if item is None:
        return ("empty", value.occurrence)
    return ("item", _item_type_key(item), value.occurrence)


def _item_type_key(item: Any) -> Any:
    return (
        item.kind,
        item.name,
        item.wildcard,
        item.type_name,
        item.nillable,
        item.target,
        item.target_is_name,
        None if item.child is None else _item_type_key(item.child),
        tuple(_type_key(parameter) for parameter in item.parameters),
        None if item.return_type is None else _type_key(item.return_type),
        None if item.key_type is None else _item_type_key(item.key_type),
        None if item.value_type is None else _type_key(item.value_type),
        None if item.member_type is None else _type_key(item.member_type),
    )


def _signatures_compatible(overridden: Signature, overriding: Signature) -> bool:
    if overridden.kind == "function":
        if _type_key(overridden.return_type) != _type_key(overriding.return_type):
            return False
        for left, right in zip(overridden.parameters, overriding.parameters, strict=False):
            if _type_key(left.sequence_type) != _type_key(right.sequence_type):
                return False
        if overridden.flags.get("new-each-time", "maybe") != overriding.flags.get(
            "new-each-time", "maybe"
        ):
            return False
        if overridden.flags.get("streamable") == "yes":
            if overriding.flags.get("streamable") != "yes":
                return False
            for name in ("posture", "sweep"):
                if overridden.flags.get(name) != overriding.flags.get(name):
                    return False
        return True
    if overridden.kind == "template":
        if _type_key(overridden.return_type) != _type_key(overriding.return_type):
            return False
        if not _context_items_compatible(overridden.context_item, overriding.context_item):
            return False
        left_params = {parameter.name: parameter for parameter in overridden.parameters}
        right_params = {parameter.name: parameter for parameter in overriding.parameters}
        for name, parameter in left_params.items():
            other = right_params.get(name)
            if other is None:
                if parameter.tunnel:
                    continue
                return False
            if _type_key(parameter.sequence_type) != _type_key(other.sequence_type):
                return False
            if parameter.tunnel != other.tunnel:
                return False
            if not parameter.tunnel and parameter.required != other.required:
                return False
        for name, parameter in right_params.items():
            if name not in left_params and parameter.required:
                return False
        return True
    if overridden.kind == "variable":
        return _type_key(overridden.sequence_type) == _type_key(overriding.sequence_type)
    return not (
        overridden.flags.get("streamable") == "yes" and overriding.flags.get("streamable") != "yes"
    )


def _context_items_compatible(
    left: ContextItemSignature | None, right: ContextItemSignature | None
) -> bool:
    assert left is not None and right is not None
    return left.use == right.use and _type_key(left.sequence_type) == _type_key(right.sequence_type)


@dataclass(frozen=True, slots=True)
class _Lookup:
    components: Mapping[ComponentKey, Component]
    package_name: str | None

    def component(
        self,
        kind: str,
        name: str | None,
        arity: int | None = None,
        *,
        include_hidden: bool = False,
    ) -> Component | None:
        component = self.components.get(_component_key(kind, name, arity))
        if component is None:
            return None
        if component.hidden and not include_hidden:
            return None
        return component


def _group_declarations(
    declarations: tuple[_Declaration, ...],
    descriptor: PackageDescriptor,
) -> tuple[_GroupedDeclaration, ...]:
    buckets: dict[ComponentKey, list[_Declaration]] = {}
    for declaration in declarations:
        key = _component_key(declaration.kind, declaration.name, declaration.arity)
        buckets.setdefault(key, []).append(declaration)
    grouped: list[_GroupedDeclaration] = []
    for key, entries in buckets.items():
        kind, name, arity = key
        highest = _highest_precedence(entries)
        at_highest = [entry for entry in entries if entry.precedence == highest]
        combining = kind in ("attribute-set", "key", "mode")
        if not combining and len(at_highest) > 1:
            code = {
                "template": "XTSE0660",
                "function": "XTSE0770",
                "variable": "XTSE0630",
                "accumulator": "XTSE3350",
            }[kind]
            raise _error(
                f"the component {name!r} is declared more than once at the same import precedence",
                code,
                element=at_highest[1].element,
                module=at_highest[1].module,
            )
        if combining:
            selected = tuple(
                sorted(
                    entries,
                    key=cmp_to_key(_compare_declarations),
                )
            )
        else:
            selected = tuple(sorted(at_highest, key=lambda entry: entry.order))
        declared_visibility = (
            _declared_mode_visibility(entries)
            if kind == "mode"
            else _declared_visibility(
                tuple(sorted(at_highest, key=lambda entry: entry.order)),
                descriptor,
                kind,
            )
        )
        is_param = kind == "variable" and selected[-1].element.name == _PARAM
        default = _default_visibility(descriptor, kind, is_param=is_param)
        grouped.append(
            _GroupedDeclaration(
                kind=kind,
                name=name,
                arity=arity,
                declarations=selected,
                precedence=highest,
                declared_visibility=declared_visibility,
                default_visibility=default,
                order=selected[0].order,
            )
        )
    grouped.sort(key=lambda group: group.order)
    return tuple(grouped)


def _compare_declarations(left: _Declaration, right: _Declaration) -> int:
    precedence = compare_precedence(left.precedence, right.precedence)
    if precedence:
        return precedence
    return left.order - right.order


def _declared_mode_visibility(entries: Sequence[_Declaration]) -> str | None:
    chosen: str | None = None
    chosen_precedence: tuple[int, ...] | None = None
    for entry in entries:
        value = _visibility(entry.element, entry.module, mode=True)
        if value is None:
            continue
        if chosen_precedence is None or compare_precedence(entry.precedence, chosen_precedence) > 0:
            chosen = value
            chosen_precedence = entry.precedence
            continue
        if entry.precedence == chosen_precedence and value != chosen:
            raise _error(
                "mode declarations disagree about visibility",
                "XTSE0545",
                element=entry.element,
                module=entry.module,
            )
    return chosen


def _highest_precedence(entries: Sequence[_Declaration]) -> tuple[int, ...]:
    return max(
        (entry.precedence for entry in entries),
        key=cmp_to_key(compare_precedence),
    )


def _declared_visibility(
    entries: tuple[_Declaration, ...],
    descriptor: PackageDescriptor,
    kind: str,
) -> str | None:
    if kind in ("key", "accumulator"):
        return None
    found: set[str] = set()
    for entry in entries:
        value = _visibility(entry.element, entry.module, mode=kind == "mode")
        if value is not None:
            found.add(value)
    if len(found) > 1:
        raise _error(
            "combined declarations disagree about visibility",
            "XTSE0020",
            element=entries[0].element,
            module=entries[0].module,
        )
    return next(iter(found), None)


def _default_visibility(descriptor: PackageDescriptor, kind: str, *, is_param: bool = False) -> str:
    if kind == "variable" and is_param:
        return "public"
    if kind == "mode":
        return "private" if descriptor.explicit else "public"
    if not descriptor.explicit and kind == "template":
        return "public"
    return "private"


def _build_local_components(
    descriptor: PackageDescriptor,
    groups: tuple[_GroupedDeclaration, ...],
    exposes: tuple[_ExposeRule, ...],
) -> dict[ComponentKey, str]:
    visibility: dict[ComponentKey, str] = {}
    matched: set[tuple[int, int]] = set()
    for group in groups:
        if group.kind not in _COMPONENT_KINDS:
            visibility[_component_key(group.kind, group.name, group.arity)] = (
                "private" if group.kind in ("accumulator", "key") else group.default_visibility
            )
            continue
        is_param = group.kind == "variable" and group.declarations[-1].element.name == _PARAM
        matches = [] if is_param else _matching_tokens(exposes, group.kind, group.name, group.arity)
        matched.update((rule.order, index) for rule, index, _ in matches)
        visibility[_component_key(group.kind, group.name, group.arity)] = _exposed_visibility(
            group.kind,
            group.name,
            group.declared_visibility,
            group.default_visibility,
            matches,
        )
    for rule in exposes:
        for index, token in enumerate(rule.tokens):
            if token.specific and (rule.order, index) not in matched:
                raise _error(
                    f"the exposed component selector {token.name or token.local!r} "
                    "matches no component",
                    "XTSE3020",
                    element=rule.element,
                    module=rule.module,
                )
    return visibility


def _exposed_visibility(
    kind: str,
    name: str | None,
    declared: str | None,
    default: str,
    matches: list[tuple[_ExposeRule | _AcceptRule, int, _NameToken]],
) -> str:
    explicit: tuple[_ExposeRule | _AcceptRule, _NameToken] | None = None
    namespace_wild: tuple[_ExposeRule | _AcceptRule, _NameToken] | None = None
    all_wild: tuple[_ExposeRule | _AcceptRule, _NameToken] | None = None
    for rule, _, token in _ordered_matches(matches):
        if token.specific:
            if explicit is None:
                explicit = (rule, token)
        elif token.kind == "all":
            if all_wild is None:
                all_wild = (rule, token)
        elif namespace_wild is None:
            namespace_wild = (rule, token)
    if explicit is not None:
        rule, _ = explicit
        visibility = rule.visibility
        if visibility == "abstract" and (kind == "mode" or declared != "abstract"):
            raise _error(
                f"the component {name!r} cannot be exposed as abstract",
                "XTSE3025",
                element=rule.element,
                module=rule.module,
            )
        if declared is not None:
            actual = _exposed_visibility_allowed(visibility, declared)
            if actual is None:
                raise _error(
                    f"the explicit exposure {visibility!r} is inconsistent with the declared "
                    f"visibility {declared!r}",
                    "XTSE3010",
                    element=rule.element,
                    module=rule.module,
                )
            return actual
        return visibility
    if (
        namespace_wild is not None
        and namespace_wild[0].visibility == "abstract"
        and declared != "abstract"
    ):
        rule, _ = namespace_wild
        raise _error(
            "a wildcard cannot make a component abstract",
            "XTSE3025",
            element=rule.element,
            module=rule.module,
        )
    if all_wild is not None and all_wild[0].visibility == "abstract" and declared != "abstract":
        rule, _ = all_wild
        raise _error(
            "a wildcard cannot make a component abstract",
            "XTSE3025",
            element=rule.element,
            module=rule.module,
        )
    if declared is not None:
        return declared
    if namespace_wild is not None:
        rule, _ = namespace_wild
        return rule.visibility
    if all_wild is not None:
        rule, _ = all_wild
        return rule.visibility
    return default


def _exposed_visibility_allowed(exposed: str, declared: str) -> str | None:
    if exposed == "public":
        return "public" if declared == "public" else None
    if exposed == "private":
        return "private" if declared in ("public", "private", "final") else None
    if exposed == "final":
        return "final" if declared in ("public", "final") else None
    if exposed == "abstract":
        return "abstract" if declared == "abstract" else None
    raise AssertionError("validated exposure visibility")  # pragma: no cover


def _default_accepted_visibility(used: str) -> str:
    if used in ("public", "final"):
        return "private"
    return "hidden"


def _is_parameter(component: Component) -> bool:
    return (
        component.kind == "variable"
        and component.signature is not None
        and component.signature.variable_kind == "param"
    )


def _accepted_visibility(accepted: str, used: str) -> str | None:
    table = {
        ("public", "public"): "public",
        ("private", "public"): "private",
        ("private", "final"): "private",
        ("final", "public"): "final",
        ("final", "final"): "final",
        ("abstract", "abstract"): "abstract",
        ("hidden", "public"): "hidden",
        ("hidden", "final"): "hidden",
        ("hidden", "abstract"): "hidden",
    }
    return table.get((accepted, used))


def _parse_override_declaration(
    element: EffectiveElement,
    module: EffectiveModule,
    descriptor: PackageDescriptor,
    default_mode: str | None,
    *,
    order: int,
) -> _GroupedDeclaration | None:
    del default_mode
    if element.name == _TEMPLATE and element.attribute("name") is None:
        if element.attribute("visibility") is not None:
            raise _error(
                "a template rule without a name must not have a visibility attribute",
                "XTSE0500",
                element=element,
                module=module,
            )
        return None
    parsed = _parse_declaration(element, module, order=0)
    declaration = parsed[0]
    declared = _declared_visibility(tuple(parsed), descriptor, declaration.kind)
    return _GroupedDeclaration(
        kind=declaration.kind,
        name=declaration.name,
        arity=declaration.arity,
        declarations=tuple(parsed),
        precedence=(),
        declared_visibility=declared,
        default_visibility="public" if element.name == _PARAM else "private",
        order=order,
    )


def parsed_declared_visibility(group: _GroupedDeclaration) -> str | None:
    return group.declared_visibility


def _make_component(
    group: _GroupedDeclaration,
    *,
    signature: Signature,
    visibility: str,
    declared_visibility: str | None,
    owner: str | None,
    containing: str | None,
    order: int,
    original_abstract: bool = False,
) -> Component:
    declarations = tuple(
        normalize_element(
            declaration.element,
            owner=owner,
            module=declaration.module.identity,
        )
        for declaration in group.declarations
    )
    children: list[InstructionNode] = []
    for declaration in declarations:
        children.extend(declaration.children)
    first = declarations[0]
    return Component(
        kind=group.kind,
        body=SequenceConstructor(
            children=tuple(children),
            owner=owner,
            module=first.module,
            base_uri=first.base_uri,
            location=first.location,
        ),
        declaration=first,
        name=group.name,
        arity=group.arity,
        visibility=visibility,
        declared_visibility=declared_visibility,
        owner=owner,
        containing=containing,
        module=group.declarations[0].module.identity,
        precedence=group.precedence,
        declaration_order=order,
        declarations=declarations,
        signature=signature,
        location=group.declarations[0].element.source_location(),
        original_abstract=original_abstract,
    )


def _copy_component(
    component: Component,
    *,
    containing: str | None,
    visibility: str,
) -> Component:
    return replace(
        component,
        containing=containing,
        visibility=visibility,
    )


def _bind_component(
    component: Component,
    lookup: _Lookup,
    original: Component | None,
) -> Component:
    declarations = tuple(
        _bind_instruction(
            declaration,
            lookup,
            original,
            original_abstract=component.original_abstract,
        )
        for declaration in component.declarations
    )
    children: list[InstructionNode] = []
    for declaration in declarations:
        children.extend(declaration.children)
    first = declarations[0]
    return replace(
        component,
        declaration=first,
        declarations=declarations,
        body=SequenceConstructor(
            children=tuple(children),
            owner=component.body.owner,
            module=component.body.module,
            base_uri=component.body.base_uri,
            location=component.body.location,
        ),
    )


def _bind_instruction(
    node: Instruction,
    lookup: _Lookup,
    original: Component | None,
    *,
    original_abstract: bool = False,
) -> Instruction:
    bound = _bind_node(node, lookup, original, original_abstract)
    assert isinstance(bound, Instruction)
    return bound


def _non_tunnel_parameter_names(component: Component) -> tuple[str, ...]:
    """The names of one target template's non-tunnel parameters, in order."""
    signature = component.signature
    assert signature is not None
    return tuple(parameter.name for parameter in signature.parameters if not parameter.tunnel)


def _rebind_instruction(node: Instruction, lookup: _Lookup) -> Instruction:
    rebound = _rebind_node(node, lookup)
    assert isinstance(rebound, Instruction)
    return rebound


def _bind_node(
    node: InstructionNode,
    lookup: _Lookup,
    original: Component | None,
    original_abstract: bool = False,
) -> InstructionNode:
    if isinstance(node, TextNode):
        return node
    reference: ComponentReference | None = None
    references = list(node.references)
    if node.is_xslt and node.local_name == "call-template":
        name = node.attribute("name")
        if name:
            expanded = _expand_instruction_name(name, node)
            if expanded == f"{_XSLT}original":
                if original_abstract:
                    raise _error(
                        "xsl:original cannot reference an abstract component",
                        "XTSE3075",
                        uri=node.module,
                    )
                if original is None or original.kind != "template":
                    raise _error(
                        "xsl:original is not available in this context",
                        "XTSE0650",
                        uri=node.module,
                    )
                reference = ComponentReference(
                    kind="template",
                    name=original.name,
                    owner=original.owner,
                    containing=original.containing,
                    visibility=original.visibility,
                    original=True,
                    parameter_names=_non_tunnel_parameter_names(original),
                )
            else:
                target = lookup.component("template", expanded)
                if target is None:
                    raise _error(
                        f"the named template {name!r} is not visible in this package",
                        "XTSE0650",
                        uri=node.module,
                    )
                reference = ComponentReference(
                    kind="template",
                    name=target.name,
                    owner=target.owner,
                    containing=target.containing,
                    visibility=target.visibility,
                    parameter_names=_non_tunnel_parameter_names(target),
                )
    for attribute in node.attributes:
        is_xslt_attribute = (node.is_xslt and attribute.namespace_uri is None) or (
            not node.is_xslt and attribute.namespace_uri == XSLT_NS
        )
        if attribute.local_name == "use-attribute-sets" and is_xslt_attribute:
            for token in attribute.value.split():
                expanded = _expand_instruction_name(token, node)
                if expanded == f"{_XSLT}original":
                    if original_abstract:
                        raise _error(
                            "xsl:original cannot reference an abstract component",
                            "XTSE3075",
                            uri=node.module,
                        )
                    if original is None or original.kind != "attribute-set":
                        raise _error(
                            "xsl:original is not available for this attribute set",
                            "XTSE0710",
                            uri=node.module,
                        )
                    references.append(
                        ComponentReference(
                            kind="attribute-set",
                            name=original.name,
                            owner=original.owner,
                            containing=original.containing,
                            visibility=original.visibility,
                            original=True,
                        )
                    )
                    continue
                target = lookup.component("attribute-set", expanded)
                if target is None:
                    raise _error(
                        f"the attribute set {token!r} is not visible in this package",
                        "XTSE0710",
                        uri=node.module,
                    )
                references.append(
                    ComponentReference(
                        kind="attribute-set",
                        name=target.name,
                        owner=target.owner,
                        containing=target.containing,
                        visibility=target.visibility,
                    )
                )
        is_expression_attribute = (
            node.is_xslt and attribute.local_name in _XPATH_REFERENCE_ATTRIBUTES
        ) or (
            not node.is_xslt
            and attribute.namespace_uri != XSLT_NS
            and _has_attribute_value_template(attribute.value)
        )
        if is_expression_attribute:
            references.extend(
                _original_expression_references(attribute.value, node, original, original_abstract)
            )
    children = tuple(
        _bind_node(child, lookup, original, original_abstract) for child in node.children
    )
    return replace(node, children=children, reference=reference, references=tuple(references))


def _expand_instruction_name(name: str, node: Instruction) -> str:
    return _expand_name(
        name,
        node.namespaces,
        uri=node.module,
        code="XTSE0280",
    )


def _has_attribute_value_template(source: str) -> bool:
    """Whether an attribute value contains an unescaped value-template expression."""
    index = 0
    while index < len(source):
        if source[index] != "{":
            index += 1
            continue
        if index + 1 < len(source) and source[index + 1] == "{":
            index += 2
            continue
        return True
    return False


def _original_expression_references(
    source: str,
    node: Instruction,
    original: Component | None,
    original_abstract: bool,
) -> tuple[ComponentReference, ...]:
    references: list[ComponentReference] = []
    for kind, _, _ in _scan_original_references(source, node.namespaces):
        if original_abstract:
            raise _error(
                "xsl:original cannot reference an abstract component",
                "XTSE3075",
                uri=node.module,
            )
        if original is None or original.kind != ("variable" if kind == "variable" else "function"):
            code = "XPST0008" if kind == "variable" else "XPST0017"
            raise _error(
                "xsl:original is not available in this expression",
                code,
                uri=node.module,
            )
        references.append(
            ComponentReference(
                kind=original.kind,
                name=original.name,
                arity=original.arity,
                owner=original.owner,
                containing=original.containing,
                visibility=original.visibility,
                original=True,
            )
        )
    return tuple(references)


def _scan_original_references(
    source: str, namespaces: Mapping[str, str]
) -> list[tuple[str, int, int]]:
    result: list[tuple[str, int, int]] = []
    index = 0
    quote: str | None = None
    comment_depth = 0
    while index < len(source):
        character = source[index]
        if comment_depth:
            if source.startswith("(:", index):
                comment_depth += 1
                index += 2
                continue
            if source.startswith(":)", index):
                comment_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if quote is not None:
            if character == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
            index += 1
            continue
        if source.startswith("(:", index):
            comment_depth = 1
            index += 2
            continue
        if character == "$" or character.isalpha() or character == "_":
            if character == "$":
                start = index + 1
                variable = True
            else:
                start = index
                variable = False
            end = _scan_qname_end(source, start)
            if end is not None:
                qname = source[start:end]
                if _is_xsl_original(qname, namespaces):
                    if variable:
                        result.append(("variable", index, end))
                    else:
                        suffix = source[end:].lstrip()
                        if suffix.startswith("(") or (
                            suffix.startswith("#") and len(suffix) > 1 and suffix[1].isdigit()
                        ):
                            result.append(("function", index, end))
                index = end
                continue
        index += 1
    return result


def _scan_qname_end(source: str, start: int) -> int | None:
    index = start
    if index < len(source) and source[index] == "Q" and source.startswith("Q{", index):
        close = source.find("}", index + 2)
        if close < 0:
            return None
        index = close + 1
    first = True
    while index < len(source):
        character = source[index]
        if first:
            if not (character.isalpha() or character == "_"):
                return None
            first = False
        elif not (character.isalnum() or character in "_-.·"):
            break
        index += 1
    if index < len(source) and source[index] == ":":
        local_start = index + 1
        if local_start < len(source) and (
            source[local_start].isalpha() or source[local_start] == "_"
        ):
            local_end = local_start + 1
            while local_end < len(source) and (
                source[local_end].isalnum() or source[local_end] in "_-.·"
            ):
                local_end += 1
            return local_end
    return index


def _is_xsl_original(qname: str, namespaces: Mapping[str, str]) -> bool:
    if qname.startswith("Q{"):
        close = qname.find("}")
        return close >= 0 and qname[2:close] == XSLT_NS and qname[close + 1 :] == "original"
    prefix, separator, local = qname.partition(":")
    if not separator:
        return False
    return local == "original" and namespaces.get(prefix) == XSLT_NS


def _rebind_component(component: Component, lookup: _Lookup) -> Component:
    declarations = tuple(
        _rebind_instruction(declaration, lookup) for declaration in component.declarations
    )
    children: list[InstructionNode] = []
    for declaration in declarations:
        children.extend(declaration.children)
    return replace(
        component,
        declaration=declarations[0],
        declarations=declarations,
        body=SequenceConstructor(
            children=tuple(children),
            owner=component.body.owner,
            module=component.body.module,
            base_uri=component.body.base_uri,
            location=component.body.location,
        ),
    )


def _rebind_node(node: InstructionNode, lookup: _Lookup) -> InstructionNode:
    if isinstance(node, TextNode):
        return node

    def rebind(reference: ComponentReference) -> ComponentReference:
        if reference.original:
            return reference
        target = lookup.component(
            reference.kind,
            reference.name,
            reference.arity,
            include_hidden=True,
        )
        if target is None:  # pragma: no cover - every component is copied
            return reference
        return ComponentReference(
            kind=target.kind,
            name=target.name,
            arity=target.arity,
            owner=target.owner,
            containing=target.containing,
            visibility=target.visibility,
            original=False,
            parameter_names=(
                _non_tunnel_parameter_names(target) if target.kind == "template" else ()
            ),
        )

    reference = None if node.reference is None else rebind(node.reference)
    references = tuple(rebind(item) for item in node.references)
    children = tuple(_rebind_node(child, lookup) for child in node.children)
    return replace(node, reference=reference, references=references, children=children)


def _apply_accepts(
    used: CompiledPackage,
    accepts: tuple[_AcceptRule, ...],
    *,
    containing: str | None,
) -> tuple[dict[ComponentKey, Component], set[ComponentKey]]:
    matched: set[tuple[int, int]] = set()
    result: dict[ComponentKey, Component] = {}
    specific_keys: set[ComponentKey] = set()
    for key, component in used.components.items():
        is_param = _is_parameter(component)
        default_visibility = (
            component.visibility if is_param else _default_accepted_visibility(component.visibility)
        )
        matches = (
            []
            if is_param or component.visibility == "hidden"
            else _matching_tokens(accepts, component.kind, component.name, component.arity)
        )
        matched.update((rule.order, index) for rule, index, _ in matches)
        selected: tuple[_AcceptRule, _NameToken] | None = None
        for rule, _, token in _ordered_matches(matches):
            new_visibility = _accepted_visibility(rule.visibility, component.visibility)
            if new_visibility is None:
                if token.specific:
                    raise _error(
                        f"the accept visibility {rule.visibility!r} is incompatible with "
                        f"{component.name!r}",
                        "XTSE3040",
                        element=rule.element,
                        module=rule.module,
                    )
                continue
            selected = (cast(_AcceptRule, rule), token)
            break
        if selected is None:
            result[key] = _copy_component(
                component, containing=containing, visibility=default_visibility
            )
            continue
        rule, token = selected
        new_visibility = _accepted_visibility(rule.visibility, component.visibility)
        assert new_visibility is not None
        if token.specific:
            specific_keys.add(key)
        result[key] = _copy_component(component, containing=containing, visibility=new_visibility)
    for rule in accepts:
        for index, token in enumerate(rule.tokens):
            if (
                token.specific
                and rule.kinds & (_COMPONENT_KINDS - {"mode"})
                and (rule.order, index) not in matched
            ):
                raise _error(
                    f"the accepted component selector {token.name or token.local!r} "
                    "matches no component",
                    "XTSE3030",
                    element=rule.element,
                    module=rule.module,
                )
    return result, specific_keys
