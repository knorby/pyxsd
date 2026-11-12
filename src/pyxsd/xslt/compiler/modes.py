"""XSLT 3.0 modes, template rules, and mode-layer construction.

This module owns everything mode-shaped in package compilation: raw template
rule declarations, mode property merging, rule-layer construction, accepted
mode visibility, and declared-mode checking.  Package composition supplies the
descriptor and component table; this module never builds a second graph.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cmp_to_key
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from pyxsd.locations import IssueLocation
from pyxsd.xslt.compiler.components import (
    Component,
    ComponentKey,
    _accepted_visibility,
    _AcceptRule,
    _bind_instruction,
    _cmp_precedence,
    _component_key,
    _Declaration,
    _default_accepted_visibility,
    _error,
    _expand_name,
    _GroupedDeclaration,
    _Lookup,
    _matching_tokens,
    _NameToken,
    _ordered_matches,
    _parse_priority,
    _rebind_instruction,
)
from pyxsd.xslt.compiler.instructions import (
    Instruction,
    InstructionNode,
    SequenceConstructor,
    TextNode,
    normalize_element,
)
from pyxsd.xslt.compiler.static import EffectiveElement, EffectiveModule

if TYPE_CHECKING:
    from pyxsd.xslt.compiler.packages import CompiledPackage, PackageDescriptor, _Local

ModeKey = str | None
HiddenModeKey = tuple[ModeKey, str | None]

_TYPED_MODE_VALUES: Mapping[str, str] = MappingProxyType(
    {
        "yes": "yes",
        "true": "yes",
        "1": "yes",
        "no": "no",
        "false": "no",
        "0": "no",
        "strict": "strict",
        "lax": "lax",
        "unspecified": "unspecified",
    }
)


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
    property_namespaces: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    namespaces: Mapping[str, str] = field(default_factory=dict)
    module: str | None = None
    location: IssueLocation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespaces", MappingProxyType(dict(self.namespaces)))
        object.__setattr__(
            self,
            "property_namespaces",
            MappingProxyType(
                {
                    name: MappingProxyType(dict(namespaces))
                    for name, namespaces in self.property_namespaces.items()
                }
            ),
        )

    @property
    def rules(self) -> tuple[ModeRule, ...]:
        return tuple(rule for layer in self.rule_layers for rule in layer.rules)

    @property
    def typed(self) -> str:
        """The normalized effective ``typed`` policy of this mode."""
        value = self.properties.get("typed")
        if value is None:
            return "unspecified"
        return _TYPED_MODE_VALUES[value]


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


def _merged_mode_property(
    entries: Sequence[tuple[tuple[int, ...], str, _Declaration]],
    *,
    code: str,
    message: str,
) -> tuple[str, _Declaration]:
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
    return chosen


def _merge_mode_properties(
    declarations: Sequence[_Declaration], descriptor: PackageDescriptor
) -> tuple[Mapping[str, str], Mapping[str, Mapping[str, str]]]:
    entries: dict[str, list[tuple[tuple[int, ...], str, _Declaration]]] = {}
    for declaration in declarations:
        for attribute in declaration.element.attributes:
            # Only attributes in no namespace are XSLT-defined properties of
            # xsl:mode.  An attribute in a foreign namespace is an extension
            # attribute and must be ignored (section 3.2).
            if attribute.namespace_uri is not None:
                continue
            local = attribute.local_name
            assert local is not None
            if local in ("name", "visibility") or attribute.name.startswith("_"):
                continue
            entries.setdefault(local, []).append(
                (declaration.precedence, attribute.value, declaration)
            )
    properties: dict[str, str] = {}
    property_namespaces: dict[str, Mapping[str, str]] = {}
    for local, values in entries.items():
        value, declaration = _merged_mode_property(
            values,
            code="XTSE0545",
            message=f"mode declarations disagree about {local!r}",
        )
        if local == "typed":
            value = _typed_mode_value(value, declaration)
        properties[local] = value
        property_namespaces[local] = declaration.element.in_scope_namespaces
    return MappingProxyType(properties), MappingProxyType(property_namespaces)


def _typed_mode_value(value: str, declaration: _Declaration) -> str:
    """Normalize one ``typed`` mode attribute and reject invalid values."""
    policy = _TYPED_MODE_VALUES.get(value.strip())
    if policy is None:
        raise _error(
            f"the typed attribute has an invalid value {value!r}",
            "XTSE0020",
            element=declaration.element,
            module=declaration.module,
        )
    return policy


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


def _apply_accepted_modes(
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


def _visible_mode(package: CompiledPackage, name: ModeKey) -> Mode | None:
    mode = package.modes.get(name)
    if mode is None or mode.containing != package.name:
        return None
    return mode


def _build_local_modes(
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
        properties, property_namespaces = _merge_mode_properties(group.declarations, descriptor)
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
            property_namespaces=property_namespaces,
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
            default_visibility = "private" if mode_name is None or descriptor.explicit else "public"
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
