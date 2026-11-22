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

Declaration binding, visibility, and schema-import compilation live in
:mod:`pyxsd.xslt.compiler.components`,
:mod:`pyxsd.xslt.compiler.modes`, and
:mod:`pyxsd.xslt.compiler.schema_imports`; this module owns package identity,
resolution, and the composition order that ties them together.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cmp_to_key
from types import MappingProxyType
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pyxsd.locations import IssueLocation
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt.compiler.attribute_values import require_item_type
from pyxsd.xslt.compiler.components import (
    _ACCEPT,
    _APPLY_TEMPLATES,
    _ATTRIBUTE_SET,
    _CONTEXT_ITEM,
    _EXPOSE,
    _FUNCTION,
    _OVERRIDE,
    _PARAM,
    _STYLESHEET,
    _TEMPLATE,
    _TRANSFORM,
    _USE_PACKAGE,
    _VARIABLE,
    Component,
    ComponentKey,
    ContextItemSignature,
    ParameterSignature,
    Signature,
    _apply_accepts,
    _bind_component,
    _build_local_components,
    _compare_declarations,
    _component_key,
    _copy_component,
    _Declaration,
    _error,
    _expand_name,
    _ExposeRule,
    _group_declarations,
    _GroupedDeclaration,
    _highest_precedence,
    _Lookup,
    _make_component,
    _parse_accept,
    _parse_declaration,
    _parse_expose,
    _parse_override_declaration,
    _rebind_component,
    _signature,
    _signatures_compatible,
    _yes_no,
    parsed_declared_visibility,
)
from pyxsd.xslt.compiler.components import (
    XSLT_NS as XSLT_NS,
)
from pyxsd.xslt.compiler.context import ProfileOptions, StaticContext
from pyxsd.xslt.compiler.expressions import CompiledSequenceType, compile_sequence_type
from pyxsd.xslt.compiler.instructions import (
    ComponentReference as ComponentReference,
)
from pyxsd.xslt.compiler.instructions import (
    Instruction,
    normalize_element,
)
from pyxsd.xslt.compiler.modes import (
    HiddenModeKey,
    Mode,
    ModeKey,
    ModeRule,
    ModeRuleLayer,
    _apply_accepted_modes,
    _bind_rule,
    _build_local_modes,
    _check_declared_modes,
    _expand_mode_tokens,
    _make_rule,
    _parse_priority,
    _rebind_rule,
    _resolve_default_mode,
    _RuleDeclaration,
    _visible_mode,
)
from pyxsd.xslt.compiler.modules import ModuleLoader, SourceModule
from pyxsd.xslt.compiler.schema_imports import (
    _IMPORT_SCHEMA,
    _attach_schema_pools,
    _check_schema_consistency,
    _compile_schema_import_declarations,
    _merge_schema_pools,
    _parse_schema_import,
    _SchemaImport,
)
from pyxsd.xslt.compiler.static import (
    EffectiveElement,
    EffectiveModule,
    EffectiveText,
    StaticPreprocessor,
)
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.precedence import compare_precedence
from pyxsd.xslt.serialization.parameters import expand_qname

_PACKAGE_VERSION = re.compile(
    r"^(?P<numeric>[0-9]+(?:\.[0-9]+)*)(?:-(?P<name>[A-Za-z_][\w.\-]*))?$"
)
_GLOBAL_CONTEXT_ITEM = f"{{{XSLT_NS}}}global-context-item"
_ON_COMPLETION = f"{{{XSLT_NS}}}on-completion"
_ITERATE = f"{{{XSLT_NS}}}iterate"
_INPUT_TYPE_ANNOTATIONS = frozenset({"strip", "preserve", "unspecified"})

PackageInput: TypeAlias = (
    str | os.PathLike[str] | bytes | io.IOBase | SourceModule | EffectiveModule
)
ConstructKind: TypeAlias = Literal["output", "decimal-format", "character-map"]


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


@dataclass(frozen=True, slots=True)
class GlobalContextItem:
    """One package's validated ``xsl:global-context-item`` declaration."""

    use: str
    as_source: str | None = None
    sequence_type: CompiledSequenceType | None = None
    location: IssueLocation | None = None
    module: str | None = None


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
    schema_imports: tuple[_SchemaImport, ...] = ()
    schema_pool: SchemaPool | None = None
    input_type_annotations: str | None = None
    default_validation: str | None = None
    global_context_item: GlobalContextItem | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))
        object.__setattr__(self, "modes", MappingProxyType(dict(self.modes)))
        object.__setattr__(self, "hidden_modes", MappingProxyType(dict(self.hidden_modes)))
        object.__setattr__(self, "originals", MappingProxyType(dict(self.originals)))
        object.__setattr__(self, "namespaces", MappingProxyType(dict(self.namespaces)))
        self._validate_character_maps()

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

    def output(
        self,
        name: str | None = None,
        *,
        namespaces: Mapping[str, str] | None = None,
    ) -> PackageConstruct | None:
        """Return one output declaration, resolving *name* in *namespaces*.

        The default is this package's own namespace map.  A caller passing a
        lexical name from another scope supplies that scope's in-scope
        bindings, so the QName is expanded exactly once where it was written.
        """
        wanted = (
            None
            if name is None
            else _expand_name(name, self.namespaces if namespaces is None else namespaces)
        )
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

    def character_map_pairs(
        self,
        names: str | None = None,
        *,
        origin: PackageConstruct | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Expand character-map names into ``(character, replacement)`` pairs.

        *names* is the whitespace-separated content of one ``use-character-maps``
        attribute, already expanded at the site that wrote it.  Referenced maps
        are expanded depth-first in list order; an own ``xsl:output-character``
        child then overrides a referenced mapping, so the last mapping for one
        character wins (XSLT 3.0 section 26.1).  A name that does not match a
        map in this package is ``XTSE1590`` and a self-reference is
        ``XTSE1600``.
        """
        pairs: dict[str, str] = {}
        for token in () if names is None else names.split():
            self._collect_character_map(token, pairs, (), origin)
        return tuple(pairs.items())

    def _collect_character_map(
        self,
        name: str,
        pairs: dict[str, str],
        path: tuple[str, ...],
        origin: PackageConstruct | None,
    ) -> None:
        definition = self.character_map(name)
        if definition is None:
            raise XsltStaticError(
                f"the character map {name!r} is not declared in this package",
                code="XTSE1590",
                uri=None if origin is None else origin.module,
                location=None if origin is None else origin.location,
            )
        if definition.name is not None and definition.name in path:
            raise XsltStaticError(
                f"the character map {name!r} references itself",
                code="XTSE1600",
                uri=definition.module,
                location=definition.location,
            )
        expanded_path = (*path, definition.name or name)
        for token in (definition.attribute("use-character-maps") or "").split():
            self._collect_character_map(token, pairs, expanded_path, origin)
        for declaration in definition.declarations:
            for child in declaration.elements:
                if child.local_name != "output-character":
                    continue
                character = child.attribute("character")
                replacement = child.attribute("string")
                if character is None or len(character) != 1 or replacement is None:
                    raise XsltStaticError(
                        "an xsl:output-character requires one character and a string",
                        code="XTSE0010",
                        uri=definition.module,
                        location=child.location or definition.location,
                    )
                pairs[character] = replacement

    def _validate_character_maps(self) -> None:
        """Resolve every declared map and output reference at compile time."""
        for definition in self.character_maps:
            self.character_map_pairs(definition.name, origin=definition)
        for definition in self.outputs:
            self.character_map_pairs(definition.attribute("use-character-maps"), origin=definition)

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
        self._aliases: dict[str, PackageDescriptor] = {}

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

    def register_alias(self, name: str, descriptor: PackageDescriptor) -> None:
        """Register *descriptor* under an additional, non-source package name.

        An alias is a composition identity only: it never changes the
        descriptor's own name, so a synthetic application package can use a
        stylesheet whose package identity is absent without inventing a new
        source module or rewriting its component owners.
        """
        if not isinstance(name, str) or not urlsplit(name).scheme:
            raise ValueError("a package alias must be an absolute URI")
        if not isinstance(descriptor, PackageDescriptor):
            raise TypeError("a package alias requires a PackageDescriptor")
        existing = self._aliases.get(name)
        if existing is not None and existing is not descriptor:
            raise ValueError(f"the package alias {name!r} is already registered")
        self._aliases[name] = descriptor

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
        alias = self._aliases.get(name)
        if alias is not None and wanted.matches(alias.parsed_version):
            candidates.append(alias)
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
    xsd_version: str = "1.1",
    resource_policy: Any = None,
    schema: SchemaPool | None = None,
    top_level: bool = True,
    profile: ProfileOptions | None = None,
) -> CompiledPackage:
    """Compile the last supplied source as the principal package.

    Earlier sources are registered as library packages and are reachable only
    through a matching ``xsl:use-package`` declaration.  An explicit
    ``principal`` selects a different supplied source (or loads one additional
    source) without changing that registration rule.

    *schema* is the caller's in-scope schema definitions (the
    ``XsltProcessor.compile(schema=...)`` pool); it is visible to every
    package's component signatures, exactly as the composed stylesheet merges
    it into every package's expression context.

    ``xsl:import-schema`` declarations in the resulting package closure are
    compiled through pyxsd under *xsd_version* and the explicit
    *resource_policy* before this function returns, so dependent XPath
    constructors see the complete in-scope schema definitions.

    ``top_level=False`` compiles the same closure as a library, leaving the
    final check that a top-level package contains no abstract component to the
    caller.  Python customization uses that view only to validate and bind
    rules before completing abstract components in its synthetic layer.

    ``profile`` carries the compiling processor's language identity; a native
    pre-3.0 profile gates every module of the closure against its language
    vocabulary before the package is returned.
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
    compiled = _PackageCompiler(
        resolver,
        schema=schema,
        xsd_version=xsd_version,
        resource_policy=resource_policy,
    ).compile(
        _descriptor(principal_module),
        top_level=top_level,
    )
    if profile is not None:
        _assert_native_profile_language(compiled, profile)
    compiled = _attach_schema_pools(
        compiled,
        xsd_version=xsd_version,
        resource_policy=resource_policy,
    )
    _check_schema_consistency(compiled)
    return compiled


def _assert_native_profile_language(
    package: CompiledPackage,
    profile: ProfileOptions,
) -> None:
    """Gate every module of the package closure against *profile*."""
    from pyxsd.xslt.compiler.native_profile import assert_native_profile_language
    from pyxsd.xslt.compiler.schema_imports import _package_closure

    for current in _package_closure(package):
        for module in current.modules:
            assert_native_profile_language(module, profile)


# ---------------------------------------------------------------------------
# Package manifest validation


def _validate_context_item_placement(element: EffectiveElement, module: EffectiveModule) -> None:
    """Require one leading ``xsl:context-item`` in a template's content model.

    XSLT 3.0 section 10.1.1 gives ``xsl:template`` the content model
    ``(xsl:context-item?, xsl:param*, sequence-constructor)``.  A declaration
    after a parameter, after other content, or a second declaration is
    therefore the content-model error ``XTSE0010`` rather than a silently
    ignored child.
    """
    seen = False
    content_started = False
    for child in element.children:
        if isinstance(child, EffectiveElement) and child.name == _CONTEXT_ITEM:
            if seen or content_started:
                raise _error(
                    "xsl:context-item must be the first child of xsl:template and appear once",
                    "XTSE0010",
                    element=child,
                    module=module,
                )
            seen = True
            continue
        content_started = True


def _validate_manifest_placement(module: EffectiveModule) -> None:
    root = module.root
    root_name = root.local_name

    def visit(element: EffectiveElement, parent: str | None, root_child: bool) -> None:
        if element.name == _CONTEXT_ITEM and parent != _TEMPLATE:
            raise _error(
                "xsl:context-item is only permitted as a child of xsl:template",
                "XTSE0010",
                element=element,
                module=module,
            )
        if element.name == _TEMPLATE:
            _validate_context_item_placement(element, module)
        if element.name == _ON_COMPLETION and parent != _ITERATE:
            raise _error(
                "xsl:on-completion is only permitted as a child of xsl:iterate",
                "XTSE0010",
                element=element,
                module=module,
            )
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
        elif element.name == _IMPORT_SCHEMA and not root_child:
            raise _error(
                "xsl:import-schema is only permitted as a top-level declaration",
                "XTSE0010",
                element=element,
                module=module,
            )
        for child in element.elements:
            visit(child, element.name, False)

    for child in root.elements:
        visit(child, root.name, True)


def _package_input_type_annotations(descriptor: PackageDescriptor) -> str | None:
    root = descriptor.module.root
    package_value = (
        root.attribute("input-type-annotations") if root.local_name == "package" else None
    )
    module_values: set[str] = set()
    for module in descriptor.all_modules:
        if module.root.local_name == "package":
            continue
        value = module.root.attribute("input-type-annotations")
        if value is not None:
            if value not in _INPUT_TYPE_ANNOTATIONS:
                raise _error(
                    f"invalid input-type-annotations value {value!r}",
                    "XTSE0020",
                    element=module.root,
                    module=module,
                )
            module_values.add(value)
    if len(module_values) > 1:
        raise _error(
            "stylesheet modules in one package disagree on input-type-annotations",
            "XTSE0265",
            element=root,
            module=descriptor.module,
        )
    if module_values:
        value = next(iter(module_values))
        if package_value is None and root.local_name == "package":
            raise _error(
                "a module's input-type-annotations value must also appear on xsl:package",
                "XTSE0265",
                element=root,
                module=descriptor.module,
            )
        if package_value is not None and package_value != value:
            raise _error(
                "the package and its modules disagree on input-type-annotations",
                "XTSE0265",
                element=root,
                module=descriptor.module,
            )
        return value
    if package_value is not None:
        if package_value not in _INPUT_TYPE_ANNOTATIONS:
            raise _error(
                f"invalid input-type-annotations value {package_value!r}",
                "XTSE0020",
                element=root,
                module=descriptor.module,
            )
        return package_value
    return None


def _package_global_context_item(
    descriptor: PackageDescriptor,
    schema: SchemaPool | None,
) -> GlobalContextItem | None:
    """Validate one package's ``xsl:global-context-item`` declarations.

    XSLT 3.0 section 6.6.6 permits at most one declaration in a module and
    requires every module of a package to agree.  The declaration's ``as`` is an
    item type and is stored compiled so priming can signal XTTE0590 without a
    second static context.
    """
    modules = [
        descriptor.module,
        *(m for m in descriptor.all_modules if m is not descriptor.module),
    ]
    found: list[tuple[EffectiveModule, EffectiveElement]] = []
    for module in modules:
        declarations = [
            element for element in module.root.elements if element.name == _GLOBAL_CONTEXT_ITEM
        ]
        if len(declarations) > 1:
            raise _error(
                "more than one xsl:global-context-item declaration appears in one module",
                "XTSE3087",
                element=declarations[1],
                module=module,
            )
        if declarations:
            found.append((module, declarations[0]))
    if not found:
        return None
    compiled: list[tuple[EffectiveModule, EffectiveElement, str, CompiledSequenceType | None]] = []
    for module, element in found:
        use = (element.attribute("use") or "optional").strip()
        if use not in ("required", "optional", "absent"):
            raise _error(
                f"invalid xsl:global-context-item use {use!r}",
                "XTSE0020",
                element=element,
                module=module,
            )
        as_source = element.attribute("as")
        if use == "absent" and as_source is not None:
            raise _error(
                "xsl:global-context-item with use='absent' must not have an as attribute",
                "XTSE3089",
                element=element,
                module=module,
            )
        sequence_type: CompiledSequenceType | None = None
        if as_source is not None:
            context = StaticContext.for_element(module.root, schema=schema)
            sequence_type = compile_sequence_type(
                as_source,
                context,
                location=element.source_location(),
            )
            require_item_type(
                sequence_type,
                label="xsl:global-context-item/@as",
                uri=module.identity,
                location=element.source_location(),
            )
        compiled.append((module, element, use, sequence_type))
    base_module, base, base_use, base_type = compiled[0]
    for module, element, use, sequence_type in compiled[1:]:
        if use != base_use or str(sequence_type) != str(base_type):
            raise _error(
                "stylesheet modules in one package disagree on xsl:global-context-item",
                "XTSE3087",
                element=element,
                module=module,
            )
    return GlobalContextItem(
        use=base_use,
        as_source=base.attribute("as"),
        sequence_type=base_type,
        location=base.source_location(),
        module=base_module.identity,
    )


def _package_default_validation(descriptor: PackageDescriptor) -> str | None:
    value = descriptor.module.root.attribute("default-validation")
    if value is None:
        return None
    if value not in {"preserve", "strip"}:
        raise _error(
            f"invalid default-validation value {value!r}",
            "XTSE0020",
            element=descriptor.module.root,
            module=descriptor.module,
        )
    return value


# ---------------------------------------------------------------------------
# Local declarations


@dataclass(frozen=True, slots=True)
class _UsePackageSite:
    element: EffectiveElement
    module: EffectiveModule
    name: str
    version_range: str
    accepts: tuple[EffectiveElement, ...]
    overrides: tuple[EffectiveElement, ...]


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
    schema_imports: tuple[_SchemaImport, ...] = ()


def _collect(descriptor: PackageDescriptor) -> _Local:
    declarations: list[_Declaration] = []
    rules: list[_RuleDeclaration] = []
    sites: list[_UsePackageSite] = []
    exposes: list[_ExposeRule] = []
    outputs: list[_Declaration] = []
    decimal_formats: list[_Declaration] = []
    character_maps: list[_Declaration] = []
    schema_imports: list[_SchemaImport] = []
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
            if element.name == _IMPORT_SCHEMA:
                schema_imports.append(_parse_schema_import(element, module))
                order += 1
                continue
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
        schema_imports=tuple(schema_imports),
    )


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


# ---------------------------------------------------------------------------
# The compiler


class _PackageCompiler:
    def __init__(
        self,
        resolver: PackageResolver,
        *,
        schema: SchemaPool | None = None,
        xsd_version: str = "1.1",
        resource_policy: Any = None,
    ) -> None:
        self._resolver = resolver
        self._schema = schema
        self._xsd_version = xsd_version
        self._resource_policy = resource_policy
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
        imported = _compile_schema_import_declarations(
            local.schema_imports,
            xsd_version=self._xsd_version,
            resource_policy=self._resource_policy,
            uri=descriptor.module.identity,
            location=descriptor.module.root.source_location(),
        )
        schema = _merge_schema_pools(self._schema, imported)
        global_context_item = _package_global_context_item(descriptor, schema)
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
            accepted_here, specific_accept_keys = _apply_accepts(
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
            accepted_modes_here, hidden_modes_here = _apply_accepted_modes(
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
                            target_mode = _visible_mode(used_package, mode_name)
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
                    signature = _signature(parsed.declarations[0], schema=schema)
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
            signature = _signature(group.declarations[-1], schema=schema)
            components[key] = _make_component(
                group,
                signature=signature,
                visibility=local_visibility[key],
                declared_visibility=group.declared_visibility,
                owner=descriptor.name,
                containing=descriptor.name,
                order=group.order,
            )

        modes, _local_mode_rules = _build_local_modes(
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
        _check_declared_modes(descriptor, local, components, modes)

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
            schema_imports=local.schema_imports,
            schema_pool=imported,
            input_type_annotations=_package_input_type_annotations(descriptor),
            default_validation=_package_default_validation(descriptor),
            global_context_item=global_context_item,
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


def _merged_attribute_value(
    entries: Sequence[tuple[tuple[int, ...], str, _Declaration]],
    *,
    code: str,
    message: str,
) -> str:
    highest = max((entry[0] for entry in entries), key=cmp_to_key(compare_precedence))
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
                if attribute.namespace_uri is not None:
                    # A foreign-namespace attribute is an extension attribute
                    # (XSLT 3.0 section 3.2), never an output parameter.
                    continue
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
                        expanded = expand_qname(token, declaration.element.in_scope_namespaces)
                        if expanded not in merged:
                            merged.append(expanded)
                    attributes[local] = " ".join(merged)
                    continue
                if kind in ("output", "character-map") and local == "use-character-maps":
                    existing = attributes.get(local, "").split()
                    tokens = [
                        expand_qname(
                            token,
                            declaration.element.in_scope_namespaces,
                            use_default_namespace=False,
                        )
                        for token in attribute.value.split()
                    ]
                    attributes[local] = " ".join((*existing, *tokens))
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
