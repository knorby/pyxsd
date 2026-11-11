"""XSLT 3.0 package composition, component visibility, and template modes.

This module builds one immutable compiled package (or a package set rooted at a
principal package) from already preprocessed effective modules.  It composes
the separate graphs explicitly:

* ``xsl:include`` and ``xsl:import`` stay inside one package and use import
  precedence, never package visibility;
* ``xsl:use-package`` crosses a package boundary through an explicit
  :class:`PackageResolver`;
* ``xsl:expose`` and ``xsl:accept`` modify visibility without merging module
  trees; and
* ``xsl:override`` substitutes a named component, while match-only rules inside
  ``xsl:override`` extend a public mode as a separate rule layer.

The complete component graph is assembled before symbolic references are
bound.  Package names never become Python imports or implicit network
locations: a resolver only returns packages already registered by the caller or
loaded from explicitly supplied resources.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urlsplit

from pyxsd.locations import IssueLocation
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
from pyxsd.xslt.compiler.modules import ModuleLoader, SourceModule
from pyxsd.xslt.compiler.static import (
    EffectiveElement,
    EffectiveModule,
    EffectiveText,
    StaticPreprocessor,
)
from pyxsd.xslt.errors import XsltStaticError

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
_PACKAGE = f"{_XSLT}package"
_CONTEXT_ITEM = f"{_XSLT}context-item"
_APPLY_TEMPLATES = f"{_XSLT}apply-templates"

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
_PACKAGE_VERSION = re.compile(
    r"^(?P<numeric>[0-9]+(?:\.[0-9]+)*)(?:-(?P<name>[A-Za-z_][\w.\-]*))?$"
)
_ERROR_CODE = re.compile(r"\[(?:err:)?([A-Z]{4}[0-9]{4})\]")

PackageInput: TypeAlias = (
    str | os.PathLike[str] | bytes | io.IOBase | SourceModule | EffectiveModule
)
ComponentKey: TypeAlias = tuple[str, str | None, int | None]
ModeKey: TypeAlias = str | None
ConstructKind: TypeAlias = Literal["output", "decimal-format", "character-map"]


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


def _local_name(name: str) -> str:
    return name.partition("}")[2] if name.startswith("{") else name


def _namespace_of(name: str) -> str | None:
    if not name.startswith("{"):
        return None
    return name[1:].partition("}")[0]


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


def _cmp_precedence(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare import precedence: a shorter prefix is higher; later imports win."""
    if left == right:
        return 0
    limit = min(len(left), len(right))
    if left[:limit] == right[:limit]:
        return 1 if len(left) < len(right) else -1
    return 1 if left > right else -1


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


# ---------------------------------------------------------------------------
# Package versions and ranges


@dataclass(frozen=True, slots=True)
class PackageVersion:
    """One package version with the ordering rules of XSLT 3.0 section 3.6.1."""

    text: str
    components: tuple[int | str, ...]
    raw_components: tuple[int | str, ...]

    @classmethod
    def parse(
        cls,
        value: str,
        *,
        uri: str | None = None,
        location: IssueLocation | None = None,
        code: str = "XTSE0020",
    ) -> PackageVersion:
        text = value.strip()
        if not text:
            raise XsltStaticError(
                "a package version must not be empty", code=code, uri=uri, location=location
            )
        match = _PACKAGE_VERSION.fullmatch(text)
        if match is None:
            raise XsltStaticError(
                f"the package version {value!r} is not valid", code=code, uri=uri, location=location
            )
        numeric = tuple(int(part) for part in match.group("numeric").split("."))
        name = match.group("name")
        stripped = list(numeric)
        while stripped and stripped[-1] == 0:
            stripped.pop()
        components: tuple[int | str, ...] = tuple(stripped)
        if name is not None:
            components = (*components, name)
            raw = (*numeric, name)
        else:
            raw = numeric
        return cls(text=text, components=components, raw_components=tuple(raw))

    def _compare(self, other: PackageVersion) -> int:
        left = self.components
        right = other.components
        for left_part, right_part in zip(left, right, strict=False):
            if left_part == right_part:
                continue
            if isinstance(left_part, int) and isinstance(right_part, int):
                return -1 if left_part < right_part else 1
            if isinstance(left_part, str) and isinstance(right_part, str):
                return -1 if left_part < right_part else 1
            # An NCName component sorts before an integer component.
            return -1 if isinstance(left_part, str) else 1
        if len(left) == len(right):
            return 0
        longer = left if len(left) > len(right) else right
        next_part = longer[min(len(left), len(right))]
        if longer is left:
            return 1 if isinstance(next_part, int) else -1
        return -1 if isinstance(next_part, int) else 1

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PackageVersion):
            return NotImplemented
        return self._compare(other) < 0

    def __le__(self, other: object) -> bool:
        if not isinstance(other, PackageVersion):
            return NotImplemented
        return self._compare(other) <= 0

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, PackageVersion):
            return NotImplemented
        return self._compare(other) > 0

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, PackageVersion):
            return NotImplemented
        return self._compare(other) >= 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PackageVersion):
            return NotImplemented
        return self._compare(other) == 0

    def __hash__(self) -> int:
        return hash(self.components)

    def __str__(self) -> str:
        return self.text

    def prefix_matches(self, prefix: tuple[int | str, ...]) -> bool:
        """Whether this version's raw components start with *prefix*."""
        return self.raw_components[: len(prefix)] == prefix


@dataclass(frozen=True, slots=True)
class _VersionRange:
    text: str

    def matches(self, version: PackageVersion) -> bool:
        text = self.text.strip()
        if text == "*":
            return True
        return any(self._one_matches(part.strip(), version) for part in text.split(","))

    def _one_matches(self, part: str, version: PackageVersion) -> bool:
        if not part:
            raise ValueError("empty package version range")
        if part == "*":
            raise ValueError("'*' cannot be combined with another version range")
        if part.startswith("to "):
            return self._to_matches(version, part[3:].strip())
        if " to " in part:
            start_text, end_text = part.split(" to ", 1)
            start = PackageVersion.parse(start_text.strip(), code="XTSE3000")
            if version < start:
                return False
            return self._to_matches(version, end_text.strip())
        if part.endswith(".*"):
            prefix_text = part[:-2].strip()
            prefix = PackageVersion.parse(prefix_text, code="XTSE3000")
            return version.prefix_matches(prefix.raw_components)
        if part.endswith("+"):
            start = PackageVersion.parse(part[:-1].strip(), code="XTSE3000")
            return version >= start
        exact = PackageVersion.parse(part, code="XTSE3000")
        return version == exact

    def _to_matches(self, version: PackageVersion, text: str) -> bool:
        text = text.strip()
        if text.endswith(".*"):
            prefix = PackageVersion.parse(text[:-2].strip(), code="XTSE3000")
            return version.prefix_matches(prefix.raw_components)
        limit = PackageVersion.parse(text, code="XTSE3000")
        if version <= limit:
            return True
        raw = version.raw_components
        raw_limit = limit.raw_components
        return len(raw) == len(raw_limit) + 1 and isinstance(raw[-1], str)


def _parse_version_range(value: str) -> _VersionRange:
    parsed = _VersionRange(value)
    try:
        probe = PackageVersion.parse("1")
        if value.strip() != "*":
            for part in value.split(","):
                parsed._one_matches(part.strip(), probe)
    except (ValueError, XsltStaticError) as exc:
        raise XsltStaticError(str(exc), code="XTSE3000") from exc
    return parsed


# ---------------------------------------------------------------------------
# Public compiled values


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
class ModeRule:
    """One compiled template rule within one mode layer."""

    pattern: str
    body: SequenceConstructor
    declaration: Instruction
    mode: str | None = None
    priority: Decimal | None = None
    precedence: tuple[int, ...] = ()
    declaration_order: int = 0
    owner: str | None = None
    containing: str | None = None
    module: str | None = None
    name: str | None = None
    location: IssueLocation | None = None
    original: Component | None = None
    original_abstract: bool = False


@dataclass(frozen=True, slots=True)
class ModeRuleLayer:
    """Rules contributed by one package layer in precedence order."""

    owner: str | None
    containing: str | None
    rules: tuple[ModeRule, ...] = ()
    precedence: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Mode:
    """One compiled package-aware mode and its ordered rule layers."""

    name: str | None
    visibility: str
    owner: str | None
    containing: str | None
    declared: bool = False
    declared_visibility: str | None = None
    rule_layers: tuple[ModeRuleLayer, ...] = ()
    properties: Mapping[str, str] = field(default_factory=dict)
    namespaces: Mapping[str, str] = field(default_factory=dict)
    module: str | None = None
    location: IssueLocation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespaces", MappingProxyType(dict(self.namespaces)))

    @property
    def rules(self) -> tuple[ModeRule, ...]:
        return tuple(rule for layer in self.rule_layers for rule in layer.rules)


@dataclass(frozen=True, slots=True)
class PackageConstruct:
    """One package-local output, decimal format, or character map."""

    kind: ConstructKind
    name: str | None
    attributes: Mapping[str, str]
    declarations: tuple[Instruction, ...]
    owner: str | None
    module: str | None = None
    precedence: tuple[int, ...] = ()
    location: IssueLocation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def attribute(self, name: str) -> str | None:
        return self.attributes.get(name)


@dataclass(frozen=True, slots=True)
class UsedPackage:
    """One resolved ``xsl:use-package`` dependency."""

    name: str
    version: str
    requested_version: str


HiddenModeKey = tuple[ModeKey, str | None]


@dataclass(frozen=True, slots=True)
class CompiledPackage:
    """One completely composed, immutable package.

    ``modes`` holds the modes that are visible inside the package.  Hidden
    copies accepted from used packages are retained in ``hidden_modes`` so the
    component graph remains complete without making their names visible or
    preventing a local mode from reusing the name.
    """

    name: str | None
    version: str
    kind: Literal["explicit", "implicit"]
    modules: tuple[EffectiveModule, ...]
    components: Mapping[ComponentKey, Component]
    modes: Mapping[ModeKey, Mode]
    hidden_modes: Mapping[HiddenModeKey, Mode] = field(default_factory=dict)
    used_packages: tuple[UsedPackage, ...] = ()
    dependencies: tuple[CompiledPackage, ...] = ()
    outputs: tuple[PackageConstruct, ...] = ()
    decimal_formats: tuple[PackageConstruct, ...] = ()
    character_maps: tuple[PackageConstruct, ...] = ()
    originals: Mapping[ComponentKey, Component] = field(default_factory=dict)
    namespaces: Mapping[str, str] = field(default_factory=dict)
    default_mode: str | None = None
    location: IssueLocation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))
        object.__setattr__(self, "modes", MappingProxyType(dict(self.modes)))
        object.__setattr__(self, "hidden_modes", MappingProxyType(dict(self.hidden_modes)))
        object.__setattr__(self, "originals", MappingProxyType(dict(self.originals)))
        object.__setattr__(self, "namespaces", MappingProxyType(dict(self.namespaces)))

    def _expand(self, name: str) -> str:
        return _expand_name(name, self.namespaces)

    def _component(
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

    def named_template(self, name: str) -> Component | None:
        return self._component("template", self._expand(name))

    def function(self, name: str, arity: int) -> Component | None:
        return self._component("function", self._expand(name), arity)

    def variable(self, name: str) -> Component | None:
        return self._component("variable", self._expand(name))

    def attribute_set(self, name: str) -> Component | None:
        return self._component("attribute-set", self._expand(name))

    def accumulator(self, name: str) -> Component | None:
        return self._component("accumulator", self._expand(name))

    def key(self, name: str) -> Component | None:
        component = self._component("key", self._expand(name))
        if component is None or component.owner != self.name:
            return None
        return component

    def component(self, kind: str, name: str, arity: int | None = None) -> Component | None:
        return self._component(kind, self._expand(name), arity)

    def mode(self, name: str | None) -> Mode | None:
        key = None if name is None or name == "#unnamed" else self._expand(name)
        mode = self.modes.get(key)
        if mode is None or mode.visibility == "hidden":
            return None
        return mode

    def original_target(
        self,
        name: str,
        *,
        kind: str = "template",
        arity: int | None = None,
    ) -> Component | None:
        return self.originals.get(_component_key(kind, self._expand(name), arity))

    def output(self, name: str | None = None) -> PackageConstruct | None:
        wanted = None if name is None else self._expand(name)
        for definition in self.outputs:
            if definition.name == wanted:
                return definition
        return None

    def decimal_format(self, name: str | None = None) -> PackageConstruct | None:
        wanted = None if name is None else self._expand(name)
        for definition in self.decimal_formats:
            if definition.name == wanted:
                return definition
        return None

    def character_map(self, name: str) -> PackageConstruct | None:
        wanted = self._expand(name)
        for definition in self.character_maps:
            if definition.name == wanted:
                return definition
        return None

    @property
    def keys(self) -> Mapping[str, Component]:
        return MappingProxyType(
            {
                component.name: component
                for component in self.components.values()
                if component.kind == "key"
                and component.owner == self.name
                and component.name is not None
            }
        )


def _component_key(kind: str, name: str | None, arity: int | None = None) -> ComponentKey:
    return (kind, name, arity if kind == "function" else None)


# ---------------------------------------------------------------------------
# Package descriptors and resolver


@dataclass(frozen=True, slots=True)
class PackageDescriptor:
    """One registered package and its preprocessed module closure."""

    module: EffectiveModule
    name: str | None
    version: str
    parsed_version: PackageVersion
    explicit: bool
    declared_modes: bool
    default_mode: str | None
    manifest_modules: tuple[EffectiveModule, ...]
    all_modules: tuple[EffectiveModule, ...]

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.name, self.version)


def _walk_modules(
    module: EffectiveModule,
    *,
    via_import: bool = False,
) -> Iterator[tuple[EffectiveModule, bool]]:
    yield module, via_import
    for included in module.includes:
        yield from _walk_modules(included, via_import=via_import)
    for imported in module.imports:
        yield from _walk_modules(imported, via_import=True)


def _manifest_modules(module: EffectiveModule) -> tuple[EffectiveModule, ...]:
    result: list[EffectiveModule] = [module]
    for included in module.includes:
        result.extend(_manifest_modules(included))
    return tuple(result)


def _descriptor(module: EffectiveModule) -> PackageDescriptor:
    root = module.root
    explicit = root.local_name == "package"
    name: str | None = None
    version = "1"
    if explicit:
        declared_name = root.attribute("name")
        if declared_name:
            if not urlsplit(declared_name).scheme:
                raise _error(
                    f"the package name {declared_name!r} must be an absolute URI",
                    "XTSE0020",
                    element=root,
                    module=module,
                )
            name = declared_name
        declared_version = root.attribute("package-version")
        if declared_version is not None:
            version = PackageVersion.parse(
                declared_version, uri=module.identity, location=root.source_location()
            ).text
    parsed_version = PackageVersion.parse(
        version, uri=module.identity, location=root.source_location()
    )
    declared_modes = _yes_no(
        root.attribute("declared-modes"),
        explicit,
        element=root,
        module=module,
        attribute="declared-modes",
    )
    default_mode = root.attribute("default-mode")
    if default_mode == "#default":
        raise _error(
            "#default is not a valid default-mode value",
            "XTSE0020",
            element=root,
            module=module,
        )
    manifest = _manifest_modules(module)
    all_modules = tuple(current for current, _ in _walk_modules(module))
    return PackageDescriptor(
        module=module,
        name=name,
        version=version,
        parsed_version=parsed_version,
        explicit=explicit,
        declared_modes=declared_modes,
        default_mode=default_mode,
        manifest_modules=manifest,
        all_modules=all_modules,
    )


class PackageResolver:
    """An explicit registry for packages available to ``xsl:use-package``.

    Registration is the only way a package becomes visible.  The resolver never
    imports Python modules, dereferences package names, or retrieves network
    resources.
    """

    def __init__(self) -> None:
        self._packages: dict[str, list[PackageDescriptor]] = {}
        self._descriptors: list[PackageDescriptor] = []

    @classmethod
    def from_sources(
        cls,
        *sources: PackageInput,
        static_parameters: Mapping[str, Any] | None = None,
    ) -> PackageResolver:
        resolver = cls()
        for module in _load_sources(sources, static_parameters=static_parameters):
            resolver.register(module)
        return resolver

    def register(self, module: EffectiveModule | PackageDescriptor) -> PackageDescriptor:
        descriptor = module if isinstance(module, PackageDescriptor) else _descriptor(module)
        if descriptor.name is not None:
            versions = self._packages.setdefault(descriptor.name, [])
            if any(existing.parsed_version == descriptor.parsed_version for existing in versions):
                raise ValueError(
                    f"package {descriptor.name!r} version {descriptor.version!r} is already registered"
                )
            versions.append(descriptor)
            versions.sort(key=lambda entry: entry.parsed_version)
        self._descriptors.append(descriptor)
        return descriptor

    @property
    def descriptors(self) -> tuple[PackageDescriptor, ...]:
        return tuple(self._descriptors)

    def __len__(self) -> int:
        return len(self._descriptors)

    def __iter__(self) -> Iterator[PackageDescriptor]:
        return iter(self._descriptors)

    def resolve(
        self,
        name: str,
        version_range: str = "*",
        *,
        element: EffectiveElement | None = None,
        module: EffectiveModule | None = None,
    ) -> PackageDescriptor:
        if not isinstance(name, str) or not name:
            raise _error(
                "a package reference requires a name",
                "XTSE0010",
                element=element,
                module=module,
            )
        try:
            wanted = _parse_version_range(version_range)
        except XsltStaticError as exc:
            raise _error(
                f"invalid package version range {version_range!r}: {exc}",
                "XTSE3000",
                element=element,
                module=module,
            ) from exc
        candidates = [
            descriptor
            for descriptor in self._packages.get(name, ())
            if wanted.matches(descriptor.parsed_version)
        ]
        if not candidates:
            raise _error(
                f"no package matching {name!r} and version range {version_range!r} is registered",
                "XTSE3000",
                element=element,
                module=module,
            )
        return max(candidates, key=lambda descriptor: descriptor.parsed_version)

    def can_resolve(self, name: str, version_range: str = "*") -> bool:
        try:
            self.resolve(name, version_range)
        except XsltStaticError:
            return False
        return True

    def compile_library(self, name: str, version_range: str = "*") -> CompiledPackage:
        descriptor = self.resolve(name, version_range)
        return _PackageCompiler(self).compile(descriptor, top_level=False)

    def compile(self, principal: EffectiveModule | PackageDescriptor) -> CompiledPackage:
        descriptor = (
            principal if isinstance(principal, PackageDescriptor) else _descriptor(principal)
        )
        return _PackageCompiler(self).compile(descriptor, top_level=True)


# ---------------------------------------------------------------------------
# Loading and the public entry points


def _load_module(source: PackageInput, *, loader: ModuleLoader) -> SourceModule:
    if isinstance(source, SourceModule):
        return source
    if isinstance(source, bytes):
        return loader.load(io.BytesIO(source))
    if isinstance(source, str) and source.lstrip().startswith("<"):
        return loader.load(io.BytesIO(source.encode("utf-8")))
    return loader.load(source)


def _load_sources(
    sources: Iterable[PackageInput],
    *,
    static_parameters: Mapping[str, Any] | None,
) -> tuple[EffectiveModule, ...]:
    loader = ModuleLoader()
    preprocessor = StaticPreprocessor()
    result: list[EffectiveModule] = []
    for source in sources:
        if isinstance(source, EffectiveModule):
            result.append(source)
            continue
        module = _load_module(source, loader=loader)
        result.append(preprocessor.apply(module, static_parameters))
    return tuple(result)


def compile_package_set(
    *sources: PackageInput,
    resolver: PackageResolver | None = None,
    static_parameters: Mapping[str, Any] | None = None,
    principal: PackageInput | None = None,
) -> CompiledPackage:
    """Compile the last supplied source as the principal package.

    Earlier sources are registered as library packages and are reachable only
    through a matching ``xsl:use-package`` declaration.  An explicit
    ``principal`` selects a different supplied source (or loads one additional
    source) without changing that registration rule.
    """
    loader = ModuleLoader()
    preprocessor = StaticPreprocessor()
    loaded: list[tuple[PackageInput, EffectiveModule]] = []
    for source in sources:
        if isinstance(source, EffectiveModule):
            loaded.append((source, source))
            continue
        source_module = _load_module(source, loader=loader)
        loaded.append((source, preprocessor.apply(source_module, static_parameters)))

    if resolver is None:
        resolver = PackageResolver()
    for _, effective in loaded:
        resolver.register(effective)

    principal_module: EffectiveModule | None = None
    if principal is not None:
        for original, effective in loaded:
            if original is principal or original == principal:
                principal_module = effective
                break
        if principal_module is None:
            extra = _load_sources((principal,), static_parameters=static_parameters)
            principal_module = extra[0]
            resolver.register(principal_module)
    elif loaded:
        principal_module = loaded[-1][1]
    if principal_module is None:
        raise ValueError("compile_package_set requires at least one package source")
    return _PackageCompiler(resolver).compile(_descriptor(principal_module), top_level=True)


# ---------------------------------------------------------------------------
# Local declarations


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
class _RuleDeclaration:
    element: EffectiveElement
    module: EffectiveModule
    pattern: str
    mode_tokens: tuple[str, ...]
    priority: Decimal | None
    name: str | None
    order: int
    precedence: tuple[int, ...]
    default_mode: str | None


@dataclass(frozen=True, slots=True)
class _UsePackageSite:
    element: EffectiveElement
    module: EffectiveModule
    name: str
    version_range: str
    accepts: tuple[EffectiveElement, ...]
    overrides: tuple[EffectiveElement, ...]


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


@dataclass(frozen=True, slots=True)
class _RawMode:
    name: str | None
    declarations: tuple[_Declaration, ...]
    visibility: str
    declared_visibility: str | None
    declared: bool
    properties: Mapping[str, str]
    rules: tuple[_RuleDeclaration, ...]
    order: int
    module: EffectiveModule | None
    location: IssueLocation | None


@dataclass(frozen=True, slots=True)
class _Local:
    declarations: tuple[_Declaration, ...]
    rules: tuple[_RuleDeclaration, ...]
    sites: tuple[_UsePackageSite, ...]
    exposes: tuple[_ExposeRule, ...]
    outputs: tuple[_Declaration, ...]
    decimal_formats: tuple[_Declaration, ...]
    character_maps: tuple[_Declaration, ...]
    constructs: tuple[_Declaration, ...]
    mode_references: tuple[ModeKey, ...] = ()


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


def _validate_manifest_placement(module: EffectiveModule) -> None:
    root = module.root
    root_name = root.local_name

    def visit(element: EffectiveElement, parent: str | None, root_child: bool) -> None:
        if element.name == _USE_PACKAGE:
            if not (root_child and root_name in ("package", "stylesheet", "transform")):
                raise _error(
                    "xsl:use-package is only permitted in a package manifest",
                    "XTSE0010",
                    element=element,
                    module=module,
                )
        elif element.name == _EXPOSE:
            if not (root_child and root_name == "package"):
                raise _error(
                    "xsl:expose is only permitted as a child of xsl:package",
                    "XTSE0010",
                    element=element,
                    module=module,
                )
        elif element.name in (_ACCEPT, _OVERRIDE) and parent != _USE_PACKAGE:
            raise _error(
                f"{element.local_name} is only permitted as a child of xsl:use-package",
                "XTSE0010",
                element=element,
                module=module,
            )
        for child in element.elements:
            visit(child, element.name, False)

    for child in root.elements:
        visit(child, root.name, True)


def _collect(descriptor: PackageDescriptor) -> _Local:
    declarations: list[_Declaration] = []
    rules: list[_RuleDeclaration] = []
    sites: list[_UsePackageSite] = []
    exposes: list[_ExposeRule] = []
    outputs: list[_Declaration] = []
    decimal_formats: list[_Declaration] = []
    character_maps: list[_Declaration] = []
    mode_references: set[ModeKey] = set()
    manifest_ids = {id(module) for module in descriptor.manifest_modules}
    order = 0
    for module, via_import in _walk_modules(descriptor.module):
        if module is not descriptor.module and module.root.local_name == "package":
            raise _error(
                "a package cannot be included or imported as a stylesheet module",
                "XTSE0165",
                element=module.root,
                module=module,
            )
        _validate_manifest_placement(module)
        for element in module.root.elements:
            if element.name == _USE_PACKAGE:
                if via_import or id(module) not in manifest_ids:
                    raise _error(
                        "an xsl:use-package declaration cannot be reached through xsl:import",
                        "XTSE3008",
                        element=element,
                        module=module,
                    )
                sites.append(_parse_use_package(element, module))
                order += 1
                continue
            if element.name == _EXPOSE:
                exposes.append(_parse_expose(element, module, order=order))
                order += 1
                continue
            if element.name in (_STYLESHEET, _TRANSFORM):
                raise _error(
                    f"{element.local_name} cannot appear inside xsl:package",
                    "XTSE0010",
                    element=element,
                    module=module,
                )
            if element.name == _TEMPLATE and element.attribute("match") is not None:
                name = element.attribute("name")
                if not name and element.attribute("visibility") is not None:
                    raise _error(
                        "a template rule without a name must not have a visibility attribute",
                        "XTSE0500",
                        element=element,
                        module=module,
                    )
                mode_value = element.attribute("mode")
                if mode_value is not None and not mode_value.split():
                    raise _error(
                        "the mode attribute must not be empty",
                        "XTSE0550",
                        element=element,
                        module=module,
                    )
                mode_tokens = tuple(mode_value.split()) if mode_value is not None else ()
                default_mode = _resolve_default_mode(descriptor.default_mode, element, module)
                priority = _parse_priority(element, module)
                rules.append(
                    _RuleDeclaration(
                        element=element,
                        module=module,
                        pattern=element.attribute("match") or "",
                        mode_tokens=mode_tokens,
                        priority=priority,
                        name=name,
                        order=order,
                        precedence=module.precedence,
                        default_mode=default_mode,
                    )
                )
                order += 1
                if name:
                    declarations.extend(_parse_declaration(element, module, order=order))
                    order += 1
                continue
            parsed = _parse_declaration(element, module, order=order)
            for declaration in parsed:
                if declaration.kind == "output":
                    outputs.append(declaration)
                elif declaration.kind == "decimal-format":
                    decimal_formats.append(declaration)
                elif declaration.kind == "character-map":
                    character_maps.append(declaration)
                else:
                    declarations.append(declaration)
            order += 1
        for descendant in module.root.descendants:
            if not isinstance(descendant, EffectiveElement):
                continue
            if descendant.name != _APPLY_TEMPLATES:
                continue
            if descendant.raw is not None and any(
                ancestor.name == _USE_PACKAGE for ancestor in descendant.raw.ancestors
            ):
                continue
            mode_value = descendant.attribute("mode")
            if mode_value is None:
                mode_references.add(
                    _resolve_default_mode(descriptor.default_mode, descendant, module)
                )
                continue
            for token in mode_value.split():
                if token == "#current":
                    continue
                if token == "#default":
                    mode_references.add(
                        _resolve_default_mode(descriptor.default_mode, descendant, module)
                    )
                elif token == "#unnamed":
                    mode_references.add(None)
                elif token == "#all":
                    continue
                else:
                    mode_references.add(
                        _expand_name(
                            token,
                            descendant.in_scope_namespaces,
                            element=descendant,
                            module=module,
                        )
                    )
    return _Local(
        declarations=tuple(declarations),
        rules=tuple(rules),
        sites=tuple(sites),
        exposes=tuple(exposes),
        outputs=tuple(outputs),
        decimal_formats=tuple(decimal_formats),
        character_maps=tuple(character_maps),
        constructs=(),
        mode_references=tuple(sorted(mode_references, key=lambda item: (item is None, item or ""))),
    )


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


def _parse_use_package(element: EffectiveElement, module: EffectiveModule) -> _UsePackageSite:
    name = element.attribute("name")
    if not name:
        raise _error(
            "an xsl:use-package declaration requires a name",
            "XTSE0010",
            element=element,
            module=module,
        )
    version_value = element.attribute("package-version")
    version = "*" if version_value is None else version_value
    accepts: list[EffectiveElement] = []
    overrides: list[EffectiveElement] = []
    for child in element.elements:
        if child.name == _ACCEPT:
            accepts.append(child)
        elif child.name == _OVERRIDE:
            overrides.append(child)
        else:
            raise _error(
                f"{child.local_name} is not permitted as a child of xsl:use-package",
                "XTSE0010",
                element=child,
                module=module,
            )
    for text_child in element.children:
        if isinstance(text_child, EffectiveText) and text_child.value.strip():
            raise _error(
                "xsl:use-package must not contain non-whitespace text",
                "XTSE0010",
                element=element,
                module=module,
            )
    for override in overrides:
        for override_child in override.elements:
            if override_child.name not in (
                _TEMPLATE,
                _FUNCTION,
                _VARIABLE,
                _PARAM,
                _ATTRIBUTE_SET,
            ):
                raise _error(
                    f"{override_child.local_name} cannot appear in xsl:override",
                    "XTSE0010",
                    element=override_child,
                    module=module,
                )
        for override_text in override.children:
            if isinstance(override_text, EffectiveText) and override_text.value.strip():
                raise _error(
                    "xsl:override must not contain non-whitespace text",
                    "XTSE0010",
                    element=override,
                    module=module,
                )
    return _UsePackageSite(
        element=element,
        module=module,
        name=name,
        version_range=version,
        accepts=tuple(accepts),
        overrides=tuple(overrides),
    )


def _resolve_default_mode(
    value: str | None,
    element: EffectiveElement,
    module: EffectiveModule,
) -> str | None:
    if value is None or value == "#unnamed":
        return None
    if value == "#default":
        raise _error(
            "#default is not a valid default-mode value",
            "XTSE0020",
            element=element,
            module=module,
        )
    return _expand_name(value, element.in_scope_namespaces, element=element, module=module)


def _expand_mode_tokens(
    tokens: tuple[str, ...],
    default_mode: str | None,
    element: EffectiveElement,
    module: EffectiveModule,
    *,
    inside_override: bool = False,
) -> tuple[str | None, ...]:
    if not tokens:
        if inside_override and default_mode is None:
            raise _error(
                "a template rule in xsl:override cannot use the unnamed default mode",
                "XTSE3440",
                element=element,
                module=module,
            )
        return (default_mode,)
    if "#all" in tokens and (inside_override or len(tokens) != 1):
        code = "XTSE3440" if inside_override else "XTSE0550"
        raise _error(
            "#all must appear alone and only on a local template rule",
            code,
            element=element,
            module=module,
        )
    resolved: list[str | None] = []
    seen: set[str | None] = set()
    for token in tokens:
        if token in ("#unnamed", "#default", "#current"):
            if inside_override and token == "#unnamed":
                raise _error(
                    f"{token} is not permitted in an xsl:override template rule",
                    "XTSE3440",
                    element=element,
                    module=module,
                )
            if token == "#current":
                raise _error(
                    "#current is not permitted on xsl:template",
                    "XTSE0550",
                    element=element,
                    module=module,
                )
            value = default_mode if token == "#default" else None
        else:
            value = _expand_name(token, element.in_scope_namespaces, element=element, module=module)
        if inside_override and value is None and token in ("#default", "#unnamed"):
            raise _error(
                "a template rule in xsl:override cannot use the unnamed mode",
                "XTSE3440",
                element=element,
                module=module,
            )
        if value in seen:
            raise _error(
                "the mode list contains the same token more than once",
                "XTSE0550",
                element=element,
                module=module,
            )
        seen.add(value)
        resolved.append(value)
    return tuple(resolved)


# ---------------------------------------------------------------------------
# Signature construction and compatibility


def _module_context(module: EffectiveModule) -> StaticContext:
    return StaticContext.for_module(module)


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
) -> tuple[ParameterSignature, ...]:
    context = _module_context(module)
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
    return ContextItemSignature(
        use,
        _type(child, "as", context, default="item()"),
    )


def _signature(declaration: _Declaration) -> Signature:
    element = declaration.element
    module = declaration.module
    if declaration.kind == "template":
        context = _module_context(module)
        return Signature(
            kind="template",
            return_type=_type(element, "as", context, default="item()*"),
            parameters=_parameter_signatures(element, module),
            context_item=_context_item_signature(element, module, context),
        )
    if declaration.kind == "function":
        context = _module_context(module)
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
            parameters=_parameter_signatures(element, module),
            flags=flags,
        )
    if declaration.kind == "variable":
        context = _module_context(module)
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
        context = _module_context(module)
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


# ---------------------------------------------------------------------------
# The compiler


class _PackageCompiler:
    def __init__(self, resolver: PackageResolver) -> None:
        self._resolver = resolver
        self._cache: dict[tuple[str | None, str], CompiledPackage] = {}
        self._active: list[tuple[str | None, str]] = []

    def compile(self, descriptor: PackageDescriptor, *, top_level: bool) -> CompiledPackage:
        key = descriptor.key
        if not top_level and key in self._cache:
            return self._cache[key]
        if key in self._active:
            raise XsltStaticError(
                f"cyclic package dependency involving {descriptor.name or '<unnamed>'!r}",
                code="XTSE3005",
                uri=descriptor.module.identity,
                location=descriptor.module.root.source_location(),
            )
        self._active.append(key)
        try:
            compiled = self._build(descriptor, top_level=top_level)
            if not top_level:
                self._cache[key] = compiled
            return compiled
        finally:
            self._active.pop()

    # -- main composition ------------------------------------------------------

    def _build(self, descriptor: PackageDescriptor, *, top_level: bool) -> CompiledPackage:
        local = _collect(descriptor)
        groups = _group_declarations(local.declarations, descriptor)
        local_visibility = _build_local_components(descriptor, groups, local.exposes)

        accepted_components: dict[ComponentKey, Component] = {}
        accepted_modes: dict[ModeKey, Mode] = {}
        hidden_accepted_modes: dict[HiddenModeKey, Mode] = {}
        originals: dict[ComponentKey, Component] = {}
        overrides: dict[ComponentKey, Component] = {}
        override_sites: dict[ComponentKey, _UsePackageSite] = {}
        override_rule_layers: dict[ModeKey, list[ModeRule]] = {}
        used: list[UsedPackage] = []
        dependencies: list[CompiledPackage] = []
        conflict_sites: dict[ComponentKey, tuple[Component, _UsePackageSite]] = {}
        local_keys = {_component_key(group.kind, group.name, group.arity) for group in groups}

        for site in local.sites:
            used_descriptor = self._resolver.resolve(
                site.name,
                site.version_range,
                element=site.element,
                module=site.module,
            )
            used_package = self.compile(used_descriptor, top_level=False)
            dependencies.append(used_package)
            used.append(
                UsedPackage(
                    name=used_descriptor.name or "",
                    version=used_descriptor.version,
                    requested_version=site.version_range,
                )
            )
            accepts = tuple(
                _parse_accept(element, site.module, order=index)
                for index, element in enumerate(site.accepts)
            )
            accepted_here, specific_accept_keys = self._apply_accepts(
                used_package, accepts, containing=descriptor.name
            )
            for key, component in accepted_here.items():
                existing = accepted_components.get(key)
                if existing is not None:
                    if existing.visibility != "hidden" and component.visibility != "hidden":
                        raise _error(
                            f"the component {key!r} is accepted from more than one package",
                            "XTSE3050",
                            element=site.element,
                            module=site.module,
                        )
                    if component.visibility == "hidden":
                        continue
                accepted_components[key] = component
                conflict_sites[key] = (component, site)
            accepted_modes_here, hidden_modes_here = self._apply_accepted_modes(
                used_package, accepts, containing=descriptor.name
            )
            for mode_key, accepted_mode in accepted_modes_here.items():
                existing_mode = accepted_modes.get(mode_key)
                if existing_mode is not None:
                    raise _error(
                        f"the mode {mode_key!r} is accepted from more than one package",
                        "XTSE3050",
                        element=site.element,
                        module=site.module,
                    )
                accepted_modes[mode_key] = accepted_mode
            hidden_accepted_modes.update(hidden_modes_here)
            override_order = 0
            for override in site.overrides:
                override_default = _resolve_default_mode(
                    override.attribute("default-mode") or descriptor.default_mode,
                    override,
                    site.module,
                )
                for child in override.elements:
                    resolved_modes: tuple[ModeKey, ...] = ()
                    if child.name == _TEMPLATE and child.attribute("match") is not None:
                        tokens = tuple((child.attribute("mode") or "").split())
                        resolved_modes = _expand_mode_tokens(
                            tokens,
                            override_default,
                            child,
                            site.module,
                            inside_override=True,
                        )
                        for mode_name in resolved_modes:
                            target_mode = self._visible_mode(used_package, mode_name)
                            if target_mode is None or target_mode.visibility not in (
                                "public",
                                "final",
                            ):
                                raise _error(
                                    f"the mode {mode_name!r} is not public in the used package",
                                    "XTSE3060",
                                    element=child,
                                    module=site.module,
                                )
                            if target_mode.visibility == "final":
                                raise _error(
                                    f"the mode {mode_name!r} is final and cannot be extended",
                                    "XTSE3060",
                                    element=child,
                                    module=site.module,
                                )
                    parsed = _parse_override_declaration(
                        child,
                        site.module,
                        descriptor,
                        override_default,
                        order=override_order,
                    )
                    override_order += 1
                    if parsed is None:
                        for mode_name in resolved_modes:
                            override_rule_layers.setdefault(mode_name, []).append(
                                _make_rule(
                                    child,
                                    site.module,
                                    mode_name,
                                    override_default,
                                    owner=descriptor.name,
                                    containing=descriptor.name,
                                    original=None,
                                    order=override_order - 1,
                                )
                            )
                        continue
                    kind, name, arity = parsed.kind, parsed.name, parsed.arity
                    key = _component_key(kind, name, arity)
                    if key in local_keys:
                        raise _error(
                            f"the override declaration {name!r} conflicts with a local declaration",
                            "XTSE3055",
                            element=child,
                            module=site.module,
                        )
                    if key in overrides:
                        raise _error(
                            f"the component {name!r} is overridden more than once",
                            "XTSE3055",
                            element=child,
                            module=site.module,
                        )
                    target = used_package.components.get(key)
                    if target is None or target.containing != used_package.name:
                        raise _error(
                            f"the overridden component {name!r} is not declared in the used package",
                            "XTSE3058",
                            element=child,
                            module=site.module,
                        )
                    if target.visibility not in ("public", "abstract"):
                        raise _error(
                            f"the component {name!r} is not public or abstract",
                            "XTSE3060",
                            element=child,
                            module=site.module,
                        )
                    if key in specific_accept_keys:
                        raise _error(
                            f"the component {name!r} is both accepted and overridden",
                            "XTSE3051",
                            element=child,
                            module=site.module,
                        )
                    assert target.signature is not None
                    signature = _signature(parsed.declarations[0])
                    if not _signatures_compatible(target.signature, signature):
                        raise _error(
                            f"the overriding signature for {name!r} is incompatible",
                            "XTSE3070",
                            element=child,
                            module=site.module,
                        )
                    is_param = child.name == _PARAM
                    override_sites[key] = site
                    overrides[key] = _make_component(
                        parsed,
                        signature=signature,
                        visibility=parsed_declared_visibility(parsed)
                        or ("public" if is_param else "private"),
                        declared_visibility=parsed_declared_visibility(parsed),
                        owner=descriptor.name,
                        containing=descriptor.name,
                        order=parsed.order,
                        original_abstract=target.visibility == "abstract",
                    )
                    if target.visibility == "public":
                        originals[key] = _copy_component(
                            target,
                            containing=descriptor.name,
                            visibility="hidden",
                        )
                    if child.name == _TEMPLATE and child.attribute("match") is not None:
                        for mode_name in resolved_modes:
                            override_rule_layers.setdefault(mode_name, []).append(
                                _make_rule(
                                    child,
                                    site.module,
                                    mode_name,
                                    override_default,
                                    owner=descriptor.name,
                                    containing=descriptor.name,
                                    original=originals.get(
                                        _component_key(
                                            "template",
                                            _expand_name(
                                                child.attribute("name") or "",
                                                child.in_scope_namespaces,
                                                element=child,
                                                module=site.module,
                                            )
                                            if child.attribute("name")
                                            else None,
                                        )
                                    ),
                                    order=parsed.order,
                                    original_abstract=target.visibility == "abstract"
                                    if child.attribute("name")
                                    else False,
                                )
                            )
        for key, component in accepted_components.items():
            if component.visibility != "hidden" and key in local_keys:
                site = conflict_sites[key][1]
                raise _error(
                    f"the accepted component {component.name!r} conflicts with a local declaration",
                    "XTSE3050",
                    element=site.element,
                    module=site.module,
                )
        for key, override_component in overrides.items():
            accepted_component = accepted_components.get(key)
            if (
                accepted_component is not None
                and accepted_component.visibility != "hidden"
                and conflict_sites[key][1] is not override_sites.get(key)
            ):
                site = conflict_sites[key][1]
                raise _error(
                    f"the override declaration {override_component.name!r} conflicts "
                    "with a component accepted from another used package",
                    "XTSE3050",
                    element=site.element,
                    module=site.module,
                )
        components: dict[ComponentKey, Component] = dict(accepted_components)
        components.update(overrides)
        for group in groups:
            if group.kind == "mode":
                continue
            key = _component_key(group.kind, group.name, group.arity)
            signature = _signature(group.declarations[-1])
            components[key] = _make_component(
                group,
                signature=signature,
                visibility=local_visibility[key],
                declared_visibility=group.declared_visibility,
                owner=descriptor.name,
                containing=descriptor.name,
                order=group.order,
            )

        modes, _local_mode_rules = self._build_local_modes(
            descriptor,
            local,
            groups,
            accepted_modes,
            hidden_accepted_modes,
            local_visibility,
        )
        for mode_name, rules in override_rule_layers.items():
            current_mode = modes.get(mode_name)
            if current_mode is None or current_mode.visibility != "public":
                raise _error(
                    f"the mode {mode_name!r} is not public after acceptance",
                    "XTSE3060",
                    element=local.sites[0].element if local.sites else descriptor.module.root,
                    module=descriptor.module,
                )
            layer = ModeRuleLayer(
                owner=descriptor.name,
                containing=descriptor.name,
                rules=tuple(rules),
                precedence=(),
            )
            modes[mode_name] = replace(current_mode, rule_layers=(layer, *current_mode.rule_layers))

        components = self._bind_components(descriptor, components, originals)
        table = _Lookup(components, descriptor.name)
        originals = {key: _rebind_component(original, table) for key, original in originals.items()}
        modes, hidden_accepted_modes = self._bind_modes(
            descriptor, components, modes, hidden_accepted_modes
        )
        self._check_declared_modes(descriptor, local, components, modes)

        outputs = _build_constructs(
            _group_constructs(local.outputs, descriptor, "output"), "output", descriptor
        )
        decimal_formats = _build_constructs(
            _group_constructs(local.decimal_formats, descriptor, "decimal-format"),
            "decimal-format",
            descriptor,
        )
        character_maps = _build_constructs(
            _group_constructs(local.character_maps, descriptor, "character-map"),
            "character-map",
            descriptor,
        )
        if top_level:
            for key, component in components.items():
                if component.containing == descriptor.name and component.visibility == "abstract":
                    raise _error(
                        f"the top-level package contains the abstract component {key!r}",
                        "XTSE3080",
                        element=None,
                        module=descriptor.module,
                    )
        return CompiledPackage(
            name=descriptor.name,
            version=descriptor.version,
            kind="explicit" if descriptor.explicit else "implicit",
            modules=descriptor.all_modules,
            components=components,
            modes=dict(modes),
            hidden_modes=dict(hidden_accepted_modes),
            used_packages=tuple(used),
            dependencies=tuple(dependencies),
            outputs=outputs,
            decimal_formats=decimal_formats,
            character_maps=character_maps,
            originals=originals,
            namespaces=descriptor.module.root.in_scope_namespaces,
            default_mode=(
                None
                if descriptor.default_mode in (None, "#unnamed")
                else _expand_name(
                    descriptor.default_mode, descriptor.module.root.in_scope_namespaces
                )
            ),
            location=descriptor.module.root.source_location(),
        )

    # -- accepting components and modes ---------------------------------------

    def _apply_accepts(
        self,
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
                component.visibility
                if is_param
                else _default_accepted_visibility(component.visibility)
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
            result[key] = _copy_component(
                component, containing=containing, visibility=new_visibility
            )
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

    def _apply_accepted_modes(
        self,
        used: CompiledPackage,
        accepts: tuple[_AcceptRule, ...],
        *,
        containing: str | None,
    ) -> tuple[dict[ModeKey, Mode], dict[HiddenModeKey, Mode]]:
        modes: dict[ModeKey, Mode] = {}
        hidden_modes: dict[HiddenModeKey, Mode] = {}
        matched: set[tuple[int, int]] = set()
        candidates: list[tuple[ModeKey, Mode]] = list(used.modes.items())
        candidates.extend((mode.name, mode) for mode in used.hidden_modes.values())
        for key, mode in candidates:
            if mode.containing != used.name or key is None:
                continue
            default_visibility = _default_accepted_visibility(mode.visibility)
            matches = (
                []
                if mode.visibility == "hidden"
                else _matching_tokens(accepts, "mode", mode.name, None)
            )
            matched.update((rule.order, index) for rule, index, _ in matches)
            selected: tuple[_AcceptRule, _NameToken] | None = None
            for rule, _, token in _ordered_matches(matches):
                new_visibility = _accepted_visibility(rule.visibility, mode.visibility)
                if new_visibility is None:
                    if token.specific:
                        raise _error(
                            f"the accept visibility {rule.visibility!r} is incompatible with mode "
                            f"{mode.name!r}",
                            "XTSE3040",
                            element=rule.element,
                            module=rule.module,
                        )
                    continue
                selected = (cast(_AcceptRule, rule), token)
                break
            if selected is None:
                accepted = replace(mode, containing=containing, visibility=default_visibility)
            else:
                rule, token = selected
                new_visibility = _accepted_visibility(rule.visibility, mode.visibility)
                assert new_visibility is not None
                accepted = replace(mode, containing=containing, visibility=new_visibility)
            if accepted.visibility == "hidden":
                hidden_modes[(key, accepted.owner)] = accepted
            else:
                modes[key] = accepted
        for rule in accepts:
            for index, token in enumerate(rule.tokens):
                if token.specific and "mode" in rule.kinds and (rule.order, index) not in matched:
                    raise _error(
                        f"the accepted mode selector {token.name!r} matches no component",
                        "XTSE3030",
                        element=rule.element,
                        module=rule.module,
                    )
        return modes, hidden_modes

    # -- local modes -----------------------------------------------------------

    def _visible_mode(self, package: CompiledPackage, name: ModeKey) -> Mode | None:
        mode = package.modes.get(name)
        if mode is None or mode.containing != package.name:
            return None
        return mode

    def _build_local_modes(
        self,
        descriptor: PackageDescriptor,
        local: _Local,
        groups: tuple[_GroupedDeclaration, ...],
        accepted_modes: dict[ModeKey, Mode],
        hidden_accepted_modes: Mapping[HiddenModeKey, Mode],
        local_visibility: Mapping[ComponentKey, str],
    ) -> tuple[dict[ModeKey, Mode], dict[ModeKey, tuple[ModeRule, ...]]]:
        mode_groups = [group for group in groups if group.kind == "mode"]
        local_modes: dict[ModeKey, Mode] = {}
        rules_by_mode: dict[ModeKey, list[ModeRule]] = {}
        declared_mode_keys = {group.name for group in mode_groups}
        all_mode_keys: set[ModeKey] = set(declared_mode_keys)
        all_mode_keys.add(None)
        hidden_mode_keys = {name for name, _owner in hidden_accepted_modes}
        all_mode_keys.update(
            key
            for key in local.mode_references
            if key not in accepted_modes and key not in hidden_mode_keys
        )
        for rule in local.rules:
            if rule.mode_tokens == ("#all",):
                continue
            for mode_name in _expand_mode_tokens(
                rule.mode_tokens,
                rule.default_mode,
                rule.element,
                rule.module,
                inside_override=False,
            ):
                all_mode_keys.add(mode_name)
        for group in mode_groups:
            key = group.name
            properties = _merge_mode_properties(group.declarations, descriptor)
            declared_visibility = group.declared_visibility
            visibility = (
                "private"
                if key is None
                else local_visibility[_component_key("mode", group.name, group.arity)]
            )
            local_modes[key] = Mode(
                name=key,
                visibility=visibility,
                owner=descriptor.name,
                containing=descriptor.name,
                declared=True,
                declared_visibility=declared_visibility,
                properties=properties,
                namespaces=group.declarations[0].element.in_scope_namespaces,
                module=group.declarations[0].module.identity,
                location=group.declarations[0].element.source_location(),
            )
        for rule in local.rules:
            if rule.mode_tokens == ("#all",):
                tokens = tuple(sorted(all_mode_keys, key=lambda item: (item is None, item or "")))
            else:
                tokens = _expand_mode_tokens(
                    rule.mode_tokens,
                    rule.default_mode,
                    rule.element,
                    rule.module,
                    inside_override=False,
                )
            for mode_name in tokens:
                if mode_name in accepted_modes and mode_name not in local_modes:
                    raise _error(
                        f"a template rule cannot extend the used mode {mode_name!r} outside "
                        "xsl:override",
                        "XTSE3050",
                        element=rule.element,
                        module=rule.module,
                    )
                rules_by_mode.setdefault(mode_name, []).append(
                    _make_rule(
                        rule.element,
                        rule.module,
                        mode_name,
                        rule.default_mode,
                        owner=descriptor.name,
                        containing=descriptor.name,
                        original=None,
                        order=rule.order,
                        priority=rule.priority,
                    )
                )
        for mode_name, rules in rules_by_mode.items():
            if mode_name in local_modes:
                mode = local_modes[mode_name]
                local_modes[mode_name] = replace(
                    mode,
                    rule_layers=(
                        ModeRuleLayer(
                            owner=descriptor.name,
                            containing=descriptor.name,
                            rules=tuple(rules),
                            precedence=(),
                        ),
                        *mode.rule_layers,
                    ),
                )
            else:
                default_visibility = (
                    "private" if mode_name is None or descriptor.explicit else "public"
                )
                local_modes[mode_name] = Mode(
                    name=mode_name,
                    visibility=default_visibility,
                    owner=descriptor.name,
                    containing=descriptor.name,
                    declared=False,
                    namespaces=rules[0].declaration.namespaces if rules else {},
                    rule_layers=(
                        ModeRuleLayer(
                            owner=descriptor.name,
                            containing=descriptor.name,
                            rules=tuple(rules),
                            precedence=(),
                        ),
                    ),
                )
        for key, _mode in local_modes.items():
            if key in accepted_modes:
                raise _error(
                    f"the mode {key!r} is declared locally and acquired from a used package",
                    "XTSE3050",
                    element=descriptor.module.root,
                    module=descriptor.module,
                )
        modes = dict(accepted_modes)
        modes.update(local_modes)
        return modes, {name: tuple(rules) for name, rules in rules_by_mode.items()}

    def _check_declared_modes(
        self,
        descriptor: PackageDescriptor,
        local: _Local,
        components: Mapping[ComponentKey, Component],
        modes: Mapping[ModeKey, Mode],
    ) -> None:
        if not descriptor.explicit or not descriptor.declared_modes:
            return
        available = {
            key
            for key, mode in modes.items()
            if mode.containing == descriptor.name
            and (
                (mode.owner == descriptor.name and mode.declared)
                or (mode.owner != descriptor.name and mode.visibility in ("public", "final"))
            )
        }
        referenced: set[ModeKey] = set()
        for rule in local.rules:
            if rule.mode_tokens == ("#all",):
                referenced.update(
                    key
                    for key, mode in modes.items()
                    if mode.containing == descriptor.name and mode.owner == descriptor.name
                )
                continue
            referenced.update(
                _expand_mode_tokens(
                    rule.mode_tokens,
                    rule.default_mode,
                    rule.element,
                    rule.module,
                    inside_override=False,
                )
            )
        for component in components.values():
            if component.owner != descriptor.name:
                continue
            referenced.update(
                _instruction_mode_references(component.declaration, descriptor.default_mode)
            )
        for mode in modes.values():
            for mode_rule in mode.rules:
                if mode_rule.owner == descriptor.name:
                    referenced.update(
                        _instruction_mode_references(mode_rule.declaration, descriptor.default_mode)
                    )
        if descriptor.default_mode is not None:
            referenced.add(
                _resolve_default_mode(
                    descriptor.default_mode,
                    descriptor.module.root,
                    descriptor.module,
                )
            )
        for mode_name in referenced:
            if mode_name not in available:
                raise _error(
                    f"the mode {mode_name!r} is not declared in this package",
                    "XTSE3085",
                    element=descriptor.module.root,
                    module=descriptor.module,
                )

    # -- binding ---------------------------------------------------------------

    def _bind_components(
        self,
        descriptor: PackageDescriptor,
        components: dict[ComponentKey, Component],
        originals: Mapping[ComponentKey, Component],
    ) -> dict[ComponentKey, Component]:
        table = _Lookup(components, descriptor.name)
        result: dict[ComponentKey, Component] = {}
        for key, component in components.items():
            if component.owner != descriptor.name or component.hidden:
                result[key] = _rebind_component(component, table)
            else:
                result[key] = _bind_component(component, table, originals.get(key))
        return result

    def _bind_modes(
        self,
        descriptor: PackageDescriptor,
        components: dict[ComponentKey, Component],
        modes: dict[ModeKey, Mode],
        hidden_modes: Mapping[HiddenModeKey, Mode],
    ) -> tuple[dict[ModeKey, Mode], dict[HiddenModeKey, Mode]]:
        table = _Lookup(components, descriptor.name)

        def bind(mode: Mode) -> Mode:
            layers: list[ModeRuleLayer] = []
            for layer in mode.rule_layers:
                rules: list[ModeRule] = []
                for rule in layer.rules:
                    if rule.owner == descriptor.name:
                        rules.append(_bind_rule(rule, table))
                    else:
                        rules.append(_rebind_rule(rule, table))
                layers.append(replace(layer, rules=tuple(rules)))
            return replace(mode, rule_layers=tuple(layers))

        return (
            {key: bind(mode) for key, mode in modes.items()},
            {key: bind(mode) for key, mode in hidden_modes.items()},
        )


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


# ---------------------------------------------------------------------------
# Grouping, components, and modes


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
    precedence = _cmp_precedence(left.precedence, right.precedence)
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
        if chosen_precedence is None or _cmp_precedence(entry.precedence, chosen_precedence) > 0:
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
        key=cmp_to_key(_cmp_precedence),
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


def _make_rule(
    element: EffectiveElement,
    module: EffectiveModule,
    mode: ModeKey,
    default_mode: str | None,
    *,
    owner: str | None,
    containing: str | None,
    original: Component | None,
    order: int,
    priority: Decimal | None = None,
    original_abstract: bool = False,
) -> ModeRule:
    del default_mode
    declaration = normalize_element(element, owner=owner, module=module.identity)
    body = SequenceConstructor(
        children=declaration.children,
        owner=owner,
        module=declaration.module,
        base_uri=declaration.base_uri,
        location=declaration.location,
    )
    name_value = element.attribute("name")
    name = (
        _expand_name(name_value, element.in_scope_namespaces, element=element, module=module)
        if name_value
        else None
    )
    return ModeRule(
        pattern=element.attribute("match") or "",
        body=body,
        declaration=declaration,
        mode=mode,
        priority=priority if priority is not None else _parse_priority(element, module),
        precedence=module.precedence,
        declaration_order=order,
        owner=owner,
        containing=containing,
        module=module.identity,
        name=name,
        location=element.source_location(),
        original=original,
        original_abstract=original_abstract,
    )


def _merged_attribute_value(
    entries: Sequence[tuple[tuple[int, ...], str, _Declaration]],
    *,
    code: str,
    message: str,
) -> str:
    highest = max((entry[0] for entry in entries), key=cmp_to_key(_cmp_precedence))
    chosen: tuple[str, _Declaration] | None = None
    for precedence, value, declaration in entries:
        if precedence != highest:
            continue
        if chosen is None:
            chosen = (value, declaration)
            continue
        if chosen[0] != value:
            raise _error(
                message,
                code,
                element=declaration.element,
                module=declaration.module,
            )
    assert chosen is not None
    return chosen[0]


def _merge_mode_properties(
    declarations: Sequence[_Declaration], descriptor: PackageDescriptor
) -> Mapping[str, str]:
    entries: dict[str, list[tuple[tuple[int, ...], str, _Declaration]]] = {}
    for declaration in declarations:
        for attribute in declaration.element.attributes:
            local = attribute.local_name
            assert local is not None
            if local in ("name", "visibility") or attribute.name.startswith("_"):
                continue
            entries.setdefault(local, []).append(
                (declaration.precedence, attribute.value, declaration)
            )
    properties = {
        local: _merged_attribute_value(
            values,
            code="XTSE0545",
            message=f"mode declarations disagree about {local!r}",
        )
        for local, values in entries.items()
    }
    return MappingProxyType(properties)


def _instruction_mode_references(
    node: InstructionNode, default_mode_text: str | None
) -> set[ModeKey]:
    result: set[ModeKey] = set()
    if isinstance(node, TextNode):
        return result
    if default_mode_text in (None, "#unnamed"):
        default_mode: ModeKey = None
    elif default_mode_text == "#default":
        raise AssertionError("a package default-mode cannot be #default")  # pragma: no cover
    else:
        default_mode = _expand_name(default_mode_text, node.namespaces, uri=node.module)
    is_template_rule = node.local_name == "template" and node.attribute("match") is not None
    if node.is_xslt and (node.local_name == "apply-templates" or is_template_rule):
        value = node.attribute("mode")
        if value is None:
            result.add(default_mode)
        else:
            for token in value.split():
                if token == "#current":
                    continue
                if token == "#default":
                    result.add(default_mode)
                elif token == "#unnamed":
                    result.add(None)
                elif token == "#all":
                    continue
                else:
                    result.add(_expand_name(token, node.namespaces, uri=node.module))
    for child in node.children:
        result.update(_instruction_mode_references(child, default_mode_text))
    return result


# ---------------------------------------------------------------------------
# Binding and rebinding


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


def _bind_rule(rule: ModeRule, lookup: _Lookup) -> ModeRule:
    declaration = _bind_instruction(
        rule.declaration,
        lookup,
        rule.original,
        original_abstract=rule.original_abstract,
    )
    return replace(
        rule,
        declaration=declaration,
        body=SequenceConstructor(
            children=declaration.children,
            owner=rule.body.owner,
            module=rule.body.module,
            base_uri=rule.body.base_uri,
            location=rule.body.location,
        ),
    )


def _rebind_rule(rule: ModeRule, lookup: _Lookup) -> ModeRule:
    declaration = _rebind_instruction(rule.declaration, lookup)
    return replace(
        rule,
        declaration=declaration,
        body=SequenceConstructor(
            children=declaration.children,
            owner=rule.body.owner,
            module=rule.body.module,
            base_uri=rule.body.base_uri,
            location=rule.body.location,
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
        )

    reference = None if node.reference is None else rebind(node.reference)
    references = tuple(rebind(item) for item in node.references)
    children = tuple(_rebind_node(child, lookup) for child in node.children)
    return replace(node, reference=reference, references=references, children=children)


# ---------------------------------------------------------------------------
# Package-local constructs


def _group_constructs(
    declarations: tuple[_Declaration, ...],
    descriptor: PackageDescriptor,
    kind: ConstructKind,
) -> tuple[_GroupedDeclaration, ...]:
    buckets: dict[str | None, list[_Declaration]] = {}
    for declaration in declarations:
        buckets.setdefault(declaration.name, []).append(declaration)
    groups: list[_GroupedDeclaration] = []
    for name, entries in buckets.items():
        highest = _highest_precedence(entries)
        at_highest = [entry for entry in entries if entry.precedence == highest]
        if kind == "character-map":
            if len(at_highest) > 1:
                raise _error(
                    f"the character map {name!r} is declared more than once",
                    "XTSE1580",
                    element=at_highest[1].element,
                    module=at_highest[1].module,
                )
            selected = tuple(sorted(at_highest, key=lambda entry: entry.order))
        else:
            selected = tuple(
                sorted(
                    entries,
                    key=cmp_to_key(_compare_declarations),
                )
            )
        groups.append(
            _GroupedDeclaration(
                kind=kind,
                name=name,
                arity=None,
                declarations=selected,
                precedence=highest,
                declared_visibility=None,
                default_visibility="private",
                order=selected[0].order,
            )
        )
    groups.sort(key=lambda group: group.order)
    return tuple(groups)


def _build_constructs(
    groups: tuple[_GroupedDeclaration, ...],
    kind: ConstructKind,
    descriptor: PackageDescriptor,
) -> tuple[PackageConstruct, ...]:
    result: list[PackageConstruct] = []
    for group in groups:
        attributes: dict[str, str] = {}
        attribute_entries: dict[str, list[tuple[tuple[int, ...], str, _Declaration]]] = {}
        declarations: list[Instruction] = []
        for declaration in group.declarations:
            normalized = normalize_element(
                declaration.element,
                owner=descriptor.name,
                module=declaration.module.identity,
            )
            declarations.append(normalized)
            for attribute in declaration.element.attributes:
                local = attribute.local_name
                assert local is not None
                if attribute.name.startswith("_") or local == "name":
                    continue
                if kind == "output" and local in (
                    "cdata-section-elements",
                    "suppress-indentation",
                ):
                    existing = attributes.get(local, "").split()
                    merged = list(existing)
                    for token in attribute.value.split():
                        if token not in merged:
                            merged.append(token)
                    attributes[local] = " ".join(merged)
                    continue
                if kind == "output" and local == "use-character-maps":
                    existing = attributes.get(local, "").split()
                    attributes[local] = " ".join((*existing, *attribute.value.split()))
                    continue
                attribute_entries.setdefault(local, []).append(
                    (declaration.precedence, attribute.value, declaration)
                )
        code = {
            "output": "XTSE1560",
            "decimal-format": "XTSE1290",
            "character-map": "XTSE1580",
        }[kind]
        for local, entries in attribute_entries.items():
            attributes[local] = _merged_attribute_value(
                entries,
                code=code,
                message=f"conflicting {kind} attribute {local!r}",
            )
        result.append(
            PackageConstruct(
                kind=kind,
                name=group.name,
                attributes=attributes,
                declarations=tuple(declarations),
                owner=descriptor.name,
                module=group.declarations[0].module.identity,
                precedence=group.precedence,
                location=group.declarations[0].element.source_location(),
            )
        )
    return tuple(result)


__all__ = [
    "CompiledPackage",
    "Component",
    "ComponentKey",
    "ContextItemSignature",
    "HiddenModeKey",
    "Mode",
    "ModeRule",
    "ModeRuleLayer",
    "PackageConstruct",
    "PackageDescriptor",
    "PackageResolver",
    "PackageVersion",
    "ParameterSignature",
    "Signature",
    "UsedPackage",
    "compile_package_set",
]
