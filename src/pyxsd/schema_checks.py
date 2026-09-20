"""Schema legality and report passes for schema compilation.

The passes that run after the element-representative walk and the
class-generation loop live here as module-level functions over the
:class:`~pyxsd.validation.CompileContextProtocol` structural view of
:class:`~pyxsd.schema_composition.CompositionContext`: declaration
legality, identity-constraint references, default open content and
default attribute groups, substitution groups, derivation and content
model checks, type-reference and value-constraint checks. The
functions operate on the same containers the parser snapshots into the
context, so reports and bookkeeping stay visible to the compile.
"""

from __future__ import annotations

import contextlib
import logging
import weakref
from collections import Counter
from collections.abc import Callable
from typing import Any
from xml.etree import ElementTree as ET

from pyxsd.content_model import (
    Particle,
    _content_children,
    all_extension_occurrence,
    all_members,
    all_term,
    compile_content_model,
    compile_own_content,
)
from pyxsd.derivation import (
    blockTokens,
    is_valid_xsi_type,
    is_validly_derived,
)
from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    componentKind,
)
from pyxsd.namespaces import (
    XLINK_NS,
    XML_NS,
    XSD_NS,
    XSI_NS,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
)
from pyxsd.particle_derivation import (
    derived_wildcard_edc_violations,
    element_namespace,
    is_valid_particle_restriction,
    wildcard_subset,
)
from pyxsd.schema_base import SchemaBase
from pyxsd.schema_composition import _qnameLocal
from pyxsd.upa import upa_violations
from pyxsd.validation import CompileContextProtocol
from pyxsd.versioning import VC_NS
from pyxsd.wildcards import (
    PROCESS_SEVERITY,
    effective_attribute_wildcard,
    invalid_namespace_constraint,
    wildcard_specs_overlap,
)
from pyxsd.xpath_subset import XPathError
from pyxsd.xsd_data_types import (
    AnySimpleType,
    AnyType,
    Boolean,
    Integer,
    NCName,
    XsdDataType,
    xsd_value_key,
)

logger = logging.getLogger(__name__)


#: Single-slot memo for ``referenced_group_ids``: ``(schemaER, ids)`` of
#: the last schema walked. The schema ER is held by weak reference so
#: the memo cannot pin a finished compile alive for the process
#: lifetime; a dead or different schema recomputes.
_referenced_group_id_cache: tuple[weakref.ref[Any], set[int]] | None = None


class _GroupRedefineDeclaration:
    """A declaration whose built-in integer type is normalized to ``Integer``.

    Used only by the group-redefine restriction check so that narrowing
    one built-in integer type to another (``xs:int`` to ``xs:byte``) is
    not mistaken for an unrelated type change. Every other attribute and
    method delegates to the wrapped declaration.
    """

    __slots__ = ("_declaration",)

    def __init__(self, declaration: Any) -> None:
        self._declaration = declaration

    def __getattr__(self, name: str) -> Any:
        return getattr(self._declaration, name)

    def getType(self) -> Any:
        cls = self._declaration.getType()
        if cls is None:
            return None
        try:
            isInteger = issubclass(cls, Integer)
            isBoolean = issubclass(cls, Boolean)
        except TypeError:
            return cls
        return Integer if isInteger and not isBoolean else cls


def _sameIntegerFamily(first: Any, second: Any) -> bool:
    """Whether two classes are distinct types of the ``xs:integer`` family.

    XSD derives the whole family (``integer``/``long``/``int``/...) by
    restriction along one chain, but the Python storage lattice maps them
    to sibling ``int`` subclasses, so ``issubclass`` sees an unrelated
    pair. ``xs:boolean`` is stored under ``Integer`` too but is not in the
    chain.
    """
    try:
        return (
            issubclass(first, Integer)
            and issubclass(second, Integer)
            and not issubclass(first, Boolean)
            and not issubclass(second, Boolean)
        )
    except TypeError:
        return False


def _mixedIsTrue(value: str) -> bool:
    """The xs:boolean reading of a lexical ``mixed`` value."""
    return value.strip().lower() in ("true", "1")


#: ER class names for the identity-constraint definitions. Their
#: names share one symbol space scoped to the containing element
#: declaration (XSD 1.0 §3.11), unlike the per-target-namespace
#: symbol spaces of the global components.
_IDENTITY_KINDS = ("Key", "Keyref", "Unique")


def check_group_redefine_restrictions(
    ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any
) -> None:
    """Reports a group redefine that is not a valid restriction.

    XSD 1.0 §4.2.4: the model group of a redefined group must be a
    valid restriction of the original. A redefine written without a
    self reference *replaces* the original outright, so the compiled
    particle trees are compared with the same predicate used for a
    complex-type particle restriction (schL1/schL6/schL8/schO2). A
    redefine that does carry a self reference keeps pyxsd's
    extension-style semantics (the reference pulls the original in),
    which the corpus accepts for the standard pattern.
    """
    groups = getattr(schemaER, "groups", None) or {}
    suffix = "|base"
    for name, base_er in list(groups.items()):
        if not isinstance(name, str) or not name.endswith(suffix):
            continue
        derived_name = name[: -len(suffix)]
        derived = groups.get(derived_name)
        if derived is None:
            continue
        if group_redefine_has_self_reference(derived.xsdElement, derived_name):
            continue
        base_model = compile_own_content(base_er, py_xsd)
        derived_model = compile_own_content(derived, py_xsd)
        if base_model is None or derived_model is None:
            continue
        resolver = group_redefine_resolver
        try:
            reasons = list(
                is_valid_particle_restriction(
                    base_model,
                    derived_model,
                    resolver,
                    head_lookup=substitution_head_lookup(derived, py_xsd),
                    member_lookup=substitution_member_lookup(derived, py_xsd),
                )
            )
        except RecursionError:  # pragma: no cover - defensive
            continue
        for reason in reasons:
            ctx.report.add_error(
                f"the redefined group '{derived_name}' does not validly "
                f"restrict its original: {reason}",
                code="compose-invalid",
                phase="schema",
            )


def group_redefine_resolver(particle: Any) -> Any:
    """Resolves a particle to its declaration for the restriction check.

    Element declarations are wrapped so the type-subsumption clause
    treats the whole built-in integer family as one type: the Python
    storage lattice makes ``xs:byte``/``xs:short``/``xs:int`` siblings
    rather than a derivation chain, so a redefined group that narrows
    ``xs:int`` to ``xs:byte`` (schH1/schH2, valid) would otherwise be
    reported. Unrelated built-ins (``xs:string`` over ``xs:int``) and
    user types still compare normally.
    """
    declaration = getattr(particle, "descriptor", None)
    if declaration is None:
        return None
    return _GroupRedefineDeclaration(declaration)


def group_redefine_has_self_reference(declaration: Any, name: str) -> bool:
    """Whether a redefined group refers to itself (or its base copy)."""
    candidates = {name, f"{name}|base"}
    for element in declaration.iter():
        if not isinstance(element.tag, str):
            continue
        if element.tag.split("}")[-1] != "group":
            continue
        ref = element.get("ref")
        if ref and _qnameLocal(ref) in candidates:
            return True
    return False


def report_declaration_issues(ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any) -> None:
    """Reports declarations that cannot carry a usable name and
    identity constraints with illegal children.

    A missing or empty ``name`` on these components is a schema
    error; the ER run deliberately tolerates it so the rest of a
    large schema can still load. ``ref`` sites (elements, attributes,
    groups, attribute groups) borrow the referred declaration's name
    and are skipped.
    """
    namedKinds = (
        "Element",
        "Attribute",
        "ComplexType",
        "SimpleType",
        "Group",
        "AttributeGroup",
        "Key",
        "Keyref",
        "Unique",
        "Notation",
    )
    seen: set[int] = set()
    seenIds: dict[str, Any] = dict(ctx.directive_ids)
    declared: dict[tuple[str, Any, str], Any] = {}
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if (
            type(er).__name__ in namedKinds
            and not er.name
            and getattr(er, "ref", None) is None
            and not getattr(er, "isElementRef", False)
        ):
            ctx.report.add_error(
                f"{type(er).__name__} declaration is missing a name",
                code="declaration-name",
            )
        check_child_grammar(ctx, er)
        check_schema_namespaced_attributes(ctx, er)
        check_declaration_id(ctx, er, seenIds)
        check_duplicate_name(ctx, er, declared)
        er.checkDeclarationLegality()
        report_content_model_issues(ctx, er, py_xsd)
        misplacement = getattr(er, "misplacement", None)
        if misplacement is not None:
            code, message = misplacement
            ctx.report.add_error(message, code=code)
        stack.extend(getattr(er, "processedChildren", None) or ())


def apply_default_open_content(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Attaches each schema document's default open content to its types.

    XSD 1.1 §3.4.2.4: the ``xs:defaultOpenContent`` child of the
    ``xs:schema`` ancestor element supplies the {open content} of a
    complex type with no explicit ``xs:openContent``, when the type's
    explicit content type is non-empty or ``appliesToEmpty`` is true.
    The default is scoped to the schema *document* a type is declared
    in, so a type spliced in from an include/import looks up its own
    document's default (which may be absent), never the host's.
    """
    mainDefault = getattr(schemaER, "defaultOpenContent", None)
    composedDefaults = composed_default_open_content(ctx)
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "ComplexType":
            sourceRoot = ctx.composed_schema_roots.get(id(er.xsdElement))
            default = mainDefault if sourceRoot is None else composedDefaults.get(id(sourceRoot))
            if default is not None and er.acceptsDefaultOpenContent(
                appliesToEmpty=default.applies_to_empty
            ):
                er.openContent = default.component()
        stack.extend(getattr(er, "processedChildren", None) or ())


def apply_default_attributes(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Attaches each schema document's default attribute group to its types.

    XSD 1.1 §3.1.2: the ``defaultAttributes`` attribute of an
    ``xs:schema`` names a global attribute group whose {attribute uses}
    and {attribute wildcard} are added to every complex type definition
    declared in that same schema document, unless the type sets
    ``defaultAttributesApply="false"``. The default is scoped to the
    schema *document* a type is declared in (open044/open205), so a type
    spliced in from an include/import/redefine looks up its own
    document's group (which may be absent), never the host's. Types
    written inside an ``xs:override`` are scoped to the overridden
    document by ``splice_override`` (ii08/ii10).
    """
    mainGroup = resolve_default_attribute_group(ctx, schemaER.xsdElement)
    composedGroups = composed_default_attribute_groups(ctx)
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "ComplexType":
            sourceRoot = ctx.composed_schema_roots.get(id(er.xsdElement))
            group = mainGroup if sourceRoot is None else composedGroups.get(id(sourceRoot))
            if group is not None and er.defaultAttributesApplies():
                er.defaultAttributeGroup = group
        stack.extend(getattr(er, "processedChildren", None) or ())


def composed_default_attribute_groups(ctx: CompileContextProtocol) -> dict[int, Any]:
    """Resolves every composed schema document's ``defaultAttributes``.

    Returns ``id(source schema root) -> AttributeGroup | None``.
    """
    result: dict[int, Any] = {}
    roots = list({id(root): root for root in ctx.composed_schema_roots.values()}.values())
    for root in roots:
        key = id(root)
        if key in result:
            continue
        result[key] = resolve_default_attribute_group(ctx, root)
    return result


def resolve_default_attribute_group(ctx: CompileContextProtocol, root: Any) -> Any:
    """Resolves a schema document's ``defaultAttributes`` QName.

    Returns the global ``AttributeGroup`` the value names, or ``None``
    when the schema declares no default. A value that does not resolve
    to a global attribute group in the named namespace is a schema error
    (si01/open203/open204).
    """
    raw = root.get("defaultAttributes")
    if raw is None:
        return None
    value = raw.strip()
    try:
        resolved = ctx.namespace_context.resolve(root, value)
    except NamespaceError:
        ctx.report.add_error(
            f"the defaultAttributes value '{value}' uses a prefix that is not bound in scope",
            code="unknown-namespace-prefix",
            phase="schema",
        )
        return None
    local = local_name(resolved)
    uri = namespace_of(resolved)
    for entries in ctx.schema_context.components.values():
        for entry in entries:
            if type(entry).__name__ != "AttributeGroup":
                continue
            if not entry.checkTopLevelType():
                continue
            if entry.name != local:
                continue
            if uri is None or entry.getNamespace() == uri:
                return entry
    ctx.report.add_error(
        f"the defaultAttributes attribute group '{value}' could not be "
        "resolved to a global xs:attributeGroup",
        code="unknown-attributeGroup",
        phase="schema",
    )
    return None


def report_open_content_derivations(
    ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any
) -> None:
    """Reports open-content derivation violations on complex types.

    XSD 1.1 §3.4.6.2 clause 1.4.3.2.2 and §3.4.6.4 pin the relation
    between a derived type's effective open content and its base's:
    a restriction may not widen the mode/wildcard (with the
    unobservable empty-particle exception) and an extension may not
    narrow them. Runs after the default-open-content pass so a type
    that inherits its open content is compared with its final
    component. Violations are reported as ``particle-restriction``
    (the existing derivation code); unresolved bases and non-complex
    derivations are skipped.
    """
    from pyxsd.open_content import open_content_derivation_problem

    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "ComplexType":
            check_one_open_content_derivation(ctx, er, open_content_derivation_problem, py_xsd)
        stack.extend(getattr(er, "processedChildren", None) or ())


def check_one_open_content_derivation(
    ctx: CompileContextProtocol, er: Any, checker: Any, py_xsd: Any
) -> None:
    """Checks one complex type's open-content derivation, if applicable."""
    derivation = er.getDerivation()
    if derivation not in ("restriction", "extension"):
        return
    if er._firstProcessedChild(er, "SimpleContent") is not None:
        return
    base = er._baseComplexType()
    if base is None or base._firstProcessedChild(base, "SimpleContent") is not None:
        return
    derived = er.effectiveOpenContent()
    base_open = base.effectiveOpenContent()
    if derived is None and base_open is None:
        return
    reason = checker(
        derived,
        base_open,
        derivation,
        derived_model=compile_content_model(er, py_xsd),
        base_model=compile_content_model(base, py_xsd),
        target_namespace=er.getNamespace(),
        derived_variety=er._effectiveContentVariety(),
        base_variety=base._effectiveContentVariety(),
    )
    if reason is not None:
        ctx.report.add_error(
            f"type '{getattr(er, 'name', '?')}' has invalid open content: {reason}",
            code="particle-restriction",
        )


def composed_default_open_content(ctx: CompileContextProtocol) -> dict[int, Any]:
    """Parses the ``defaultOpenContent`` of every composed schema document.

    The element is not part of the spliced main tree, so its legality
    is checked here through the same helper the declaration walk uses
    for the host document. Returns ``id(source schema root) ->
    DefaultOpenContent | None``.
    """
    from pyxsd.open_content import default_open_content_element, parse_default_open_content

    result: dict[int, Any] = {}
    roots = list({id(root): root for root in ctx.composed_schema_roots.values()}.values())
    for root in roots:
        key = id(root)
        if key in result:
            continue
        element = default_open_content_element(root)
        if element is None:
            result[key] = None
            continue
        result[key] = parse_default_open_content(
            element,
            report_error=lambda message, code: ctx.report.add_error(
                message, code=code, phase="schema"
            ),
            target_namespace=root.get("targetNamespace"),
            resolve_qname=open_content_resolver(ctx, element),
        )
    return result


def open_content_resolver(ctx: CompileContextProtocol, element: Any) -> Any:
    """A ``notQName`` QName expander for a composed document's element."""
    context = ctx.namespace_context

    def resolve(token: str) -> str:
        return context.resolve(element, token)

    return resolve


def check_keyref_references(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Checks every keyref's ``refer`` and every constraint
    reference site's ``ref`` against the schema's constraints.

    XSD 1.0 §3.11.5 / 1.1 §3.13.5: the ``refer`` QName must
    resolve to a *key* or *unique* identity-constraint definition
    (a keyref is not a valid target, idH035) and the keyref must
    carry the same number of fields as the constraint it
    references (idH013/idH014). ``refer`` resolves as a component
    QName: a prefix through the declaration site's in-scope
    bindings, an unprefixed name through the declaration site's
    default namespace — overridden by a declared
    ``xpathDefaultNamespace`` (self → constraint → schema); the
    match against the Key and Unique definitions is by Clark
    name. The XSD 1.1 ``ref`` form must resolve to a constraint
    of the same category (§3.11.3.5); the site then acts as its
    target at instance phase. Failures are schema-phase
    ``identity-refer`` errors. The resolved names are recorded on
    the constraints (``constraintClark``/``referClark``/
    ``borrowedFrom``) for the instance phase's namespace-accurate
    scope matching.
    """
    keys: dict[str, Any] = {}
    named: dict[str, dict[str, Any]] = {"Key": {}, "Unique": {}, "Keyref": {}}
    keyrefs: list[Any] = []
    refSites: list[Any] = []
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        kind = type(er).__name__
        if kind in ("Key", "Unique", "Keyref"):
            if getattr(er, "isConstraintRef", False):
                refSites.append(er)
            elif kind in ("Key", "Unique"):
                name = getattr(er, "constraintName", None)
                if name:
                    clarkName = er._constraintClarkName()
                    er.constraintClark = clarkName
                    if clarkName:
                        keys.setdefault(clarkName, er)
                        named[kind].setdefault(clarkName, er)
                        # Redefine machinery renames base copies with
                        # "|" ("base|Key|n"); index those under their
                        # final segment too so a refer to the original
                        # name still resolves.
                        if "|" in name:
                            alias = clark(namespace_of(clarkName), name.split("|")[-1])
                            keys.setdefault(alias, er)
                            named[kind].setdefault(alias, er)
            else:
                try:
                    er.referClark = er._referClarkName()
                except XPathError as exc:
                    report_invalid_constraint_default_namespace(ctx, er, exc)
                    continue
                keyrefs.append(er)
                clarkName = er._constraintClarkName()
                if clarkName:
                    named[kind].setdefault(clarkName, er)
        stack.extend(getattr(er, "processedChildren", None) or ())
    resolve_constraint_ref_sites(ctx, refSites, named)
    for keyref in keyrefs:
        refer = getattr(keyref, "refer", "") or ""
        local = refer.split(":")[-1].strip()
        if not local:
            # An empty refer is reported by the declaration
            # legality walk.
            continue
        referClark = getattr(keyref, "referClark", None)
        resolved = keys.get(referClark) if referClark else None
        if resolved is None:
            prefix, separator, _ = refer.partition(":")
            if separator and referClark is None:
                message = (
                    f"keyref '{keyref.constraintName}': the prefix '{prefix}' of "
                    f"refer '{refer}' is not declared in the keyref's scope"
                )
            else:
                message = (
                    f"keyref '{keyref.constraintName}' refers to '{refer}', but no "
                    "key or unique with that name is declared in the schema"
                )
            ctx.report.add_error(
                message,
                code="identity-refer",
                element=getattr(keyref, "rawTag", None) or "keyref",
                phase="schema",
            )
            continue
        if len(keyref.fieldPaths) != len(resolved.fieldPaths):
            ctx.report.add_error(
                f"keyref '{keyref.constraintName}' carries {len(keyref.fieldPaths)} "
                f"field(s), but the referenced {type(resolved).__name__.lower()} "
                f"'{resolved.constraintName}' carries {len(resolved.fieldPaths)}",
                code="identity-refer",
                element=getattr(keyref, "rawTag", None) or "keyref",
                phase="schema",
            )


def resolve_constraint_ref_sites(
    ctx: CompileContextProtocol, refSites: list[Any], named: dict[str, dict[str, Any]]
) -> None:
    """Resolves XSD 1.1 constraint reference sites to their targets.

    A ``<key>``, ``<unique>`` or ``<keyref>`` carrying ``ref``
    names a constraint of its own category (§3.11.3.5) and acts as
    that constraint: ``borrowedFrom`` records the final target so
    the instance phase borrows its name, selector, fields and
    ``refer``. A reference that does not resolve — no such name,
    or the name only exists for a different category — is a
    schema-phase ``identity-refer`` error and the site is left
    unborrowed (the instance phase skips it).
    """
    for site in refSites:
        kind = type(site).__name__
        where = getattr(site, "rawTag", None) or kind.lower()
        ref = (site.tagAttributes.get("ref") or "").strip()
        try:
            clarkName = site._refClarkName()
        except XPathError as exc:
            report_invalid_constraint_default_namespace(ctx, site, exc)
            continue
        target = named[kind].get(clarkName) if clarkName else None
        if target is None:
            if clarkName is None:
                prefix, separator, _ = ref.partition(":")
                detail = (
                    f"the prefix '{prefix}' is not declared in the constraint's scope"
                    if separator
                    else "the reference is empty"
                )
            else:
                detail = (
                    "no identity constraint of this category is declared "
                    "under that name in the schema"
                )
            ctx.report.add_error(
                f"<{where}> reference '{ref}' does not resolve: {detail}",
                code="identity-refer",
                element=where,
                phase="schema",
            )
            continue
        site.borrowedFrom = target


def report_invalid_constraint_default_namespace(
    ctx: CompileContextProtocol, er: Any, exc: XPathError
) -> None:
    """Reports an unusable ``xpathDefaultNamespace`` on a constraint.

    The selector/field path reports the same condition as
    ``xpath-invalid``; a ``refer``/``ref`` QName under the same
    value must fail identically rather than surfacing as a
    resolution failure.
    """
    kind = type(er).__name__
    where = getattr(er, "rawTag", None) or kind.lower()
    ctx.report.add_error(
        f"<{where}> carries an invalid xpathDefaultNamespace: {exc}",
        code="xpath-invalid",
        element=where,
        phase="schema",
    )


#: ``xs:anyType``'s effective content: a mixed sequence holding an
#: unrestricted wildcard. It stands in for the built-in type's model
#: during extension-structure checks (the built-in has no compiled
#: class model); it is neither empty nor an ``all``, so an ``all``
#: suffix over it is reported while sequence/choice suffixes are not.
_ANY_TYPE_CONTENT = Particle("sequence", 1, 1, [Particle("any")])

#: Compositor ERs whose particle sets the content-model sweep checks.
_COMPOSITOR_KINDS = ("All", "Sequence", "Choice")

#: Compositors the pointless-particle rule (particlesHa) applies to.
_POINTLESS_KINDS = ("Sequence", "Choice")


def report_content_model_issues(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports duplicate or conflicting particles inside one compositor.

    Element declaration particles are collected transitively through
    nested compositor children — never through an element
    declaration's *type* (a different content model) and never
    through a group reference (spliced in later). Two particles with
    the same expanded name but different type declarations violate
    Element Declarations Consistent in any compositor; under an
    ``all`` any two particles with the same expanded name violate
    UPA (identical or not — identical duplicates elsewhere are
    deterministic and legal), as do an element in the substitution
    group of another and two overlapping wildcards. Each conflicting
    name is reported once per compositor.

    Complex types derived by restriction with a particle
    additionally run the particle-valid restriction check
    (``report_particle_restriction``, cos-particle-restrict).

    ``sequence``/``choice`` compositors with an empty particle set
    inside a group referenced with ``minOccurs="0"`` additionally
    violate the pointless-particle rule
    (``report_pointless_particle``).

    Reference sites are resolved against the document's global
    element declarations: references to the same declaration share
    its type, and a reference that cannot be resolved here (it
    names an import, say) is left out of the comparison — its type
    is unknown, and guessing from the raw QName would compare
    prefixes instead of declarations.
    """
    if type(er).__name__ in _POINTLESS_KINDS:
        report_pointless_particle(ctx, er)
    if type(er).__name__ == "ComplexType":
        report_particle_restriction(ctx, er, py_xsd)
        report_mixed_restriction(ctx, er, py_xsd)
        report_mixed_conflict(ctx, er)
        report_mixed_extension(ctx, er, py_xsd)
        report_complex_content_from_simple_base(ctx, er, py_xsd)
        report_attribute_use_derivation(ctx, er, py_xsd)
        report_attribute_wildcard_restriction(ctx, er, py_xsd)
        report_extension_structure(ctx, er, py_xsd)
        report_unique_particle_attribution(ctx, er, py_xsd)
    if type(er).__name__ not in _COMPOSITOR_KINDS:
        return
    if type(er).__name__ in ("Sequence", "Choice"):
        check_wildcard_particle_overlap(ctx, er)
    rawParticles: list[Any] = []
    collect_particles(er, rawParticles, set())
    resolved = resolve_particles(rawParticles)
    byName: dict[tuple[str, str], list[tuple[str, Any]]] = {}
    for name, typeKey, particle in resolved:
        byName.setdefault(name, []).append((typeKey, particle))
    isAll = type(er).__name__ == "All"
    for (_, local), sameName in byName.items():
        if len(sameName) < 2:
            continue
        typeKeys = {typeKey for typeKey, _ in sameName}
        if len(typeKeys) > 1:
            ctx.report.add_error(
                f"element declarations consistent: element '{local}' is "
                "declared with conflicting types "
                f"{sorted(key for key in typeKeys if key) or ['(anonymous)']} "
                "in the same content model",
                code="all-rule",
            )
            continue
        if isAll:
            ctx.report.add_error(
                f"content model is ambiguous: element '{local}' appears more than once in an all",
                code="all-rule",
            )
    if isAll:
        check_all_wildcard_overlap(ctx, er)
    if isAll or type(er).__name__ == "Choice":
        # In an ``all`` or ``choice`` every alternative is live at once,
        # so a head and one of its members (or two members of one head)
        # can match the same item and violate UPA (all303,
        # particlesZ033_g). A ``sequence`` is positionally ordered, so
        # the ambiguity is decided by the UPA machinery, not here.
        check_substitution_overlap(ctx, resolved, py_xsd)
    check_substitution_edc(ctx, resolved, py_xsd)


def check_wildcard_element_edc(ctx: CompileContextProtocol, er: Any, resolved: list[Any]) -> None:
    """Reports a wildcard whose global match has a conflicting type table.

    XSD 1.1 Element Declarations Consistent (bug 11076) compares the
    *type tables* of governing declarations: a strict or lax wildcard
    that admits a globally-declared element which also appears as a
    like-named local particle makes the two declarations inconsistent
    when their ``xs:alternative`` tables differ — including one being
    absent (wild078/wild079/wild081). The narrower comparison keeps
    the historical acceptance of a wildcard over a global whose plain
    type merely differs, which the corpus does not pin.
    """
    if not resolved:
        return
    wildcards = [
        child
        for child in getattr(er, "_particleChildren", lambda: ())()
        if child.__class__.__name__ == "Any"
        and child.xsdElement.get("notNamespace") is None
        and child.xsdElement.get("notQName") is None
    ]
    wildcards = [wc for wc in wildcards if getattr(wc, "wildcardSpec", None) is not None]
    if not wildcards:
        return
    lookup = global_element_lookup(er)
    try:
        target = er.getNamespace()
    except AttributeError:
        target = None
    byName: dict[tuple[str, str], list[Any]] = {}
    for name, _typeKey, particle in resolved:
        if type(particle).__name__ == "Element":
            byName.setdefault(name, []).append(particle)
    if not byName:
        return
    for wildcard in wildcards:
        spec = wildcard.wildcardSpec
        if spec.process_contents == "skip":
            continue
        effective = spec.effective_target(target)
        for (namespace, local), particles in byName.items():
            globalName = f"{{{namespace}}}{local}" if namespace else local
            if not spec.allows_name(globalName, effective):
                continue
            globalDecl = lookup(globalName)
            if globalDecl is None:
                continue
            globalSignature = alternative_table_signature(globalDecl)
            for particle in particles:
                if alternative_table_signature(particle) != globalSignature:
                    ctx.report.add_error(
                        f"element declarations consistent: element '{local}' "
                        "matched by a wildcard is declared with a conflicting "
                        "alternative type table",
                        code="element-consistent",
                    )
                    break


def check_substitution_edc(ctx: CompileContextProtocol, resolved: list[Any], py_xsd: Any) -> None:
    """Reports a substitution member redeclared with a conflicting type.

    XSD 1.1 §3.8.6.4 (Element Declarations Consistent) compares more
    than same-named declarations: wherever a head element appears in
    a content model its substitution members stand in the same place,
    so a member that also appears explicitly must carry the type of
    the global member it duplicates. A mismatched local declaration
    (``edc.xsd``'s local ``e1: integer`` against the abstract global
    member ``e1`` of ``e: string``) therefore rejects the schema.
    Names are compared as expanded ``(namespace, local)`` pairs, so
    an unqualified local never collides with a same-spelled global
    (elemZ020).
    """
    if not resolved:
        return
    try:
        schema = resolved[0][2].getSchema()
    except AttributeError:
        return

    def nameOf(holder: Any) -> tuple[str, str]:
        return (
            element_namespace(holder) or "",
            getattr(holder, "name", None) or "",
        )

    # Effective (form-aware) names for the particles in the model: a
    # reference adopts the referred global's name, a local
    # declaration is qualified-or-not per its ``form``/the document
    # default, so an unqualified local never collides with a
    # same-spelled global (elemZ020).
    entries: list[tuple[tuple[str, str], str, Any]] = []
    for name, typeKey, particle in resolved:
        entryName = name if getattr(particle, "isElementRef", False) else nameOf(particle)
        entries.append((entryName, typeKey, particle))
    modelKeys = {name for name, _typeKey, _particle in entries}

    def headsOf(holder: Any) -> set[tuple[str, str]]:
        names: set[tuple[str, str]] = set()
        for head in holder.getSubstitutionGroupHeads(py_xsd):
            if head.startswith("{"):
                namespace, _, local = head[1:].partition("}")
                names.add((namespace, local))
            else:
                names.add((holder.getNamespace() or "", head.split(":")[-1]))
        return names

    memberDecls: dict[tuple[str, str], Any] = {}
    memberHeads: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for element in getattr(schema, "elements", None) or ():
        if type(element).__name__ != "Element" or not element.name:
            continue
        if not element.tagAttributes.get("substitutionGroup"):
            continue
        key = nameOf(element)
        memberDecls[key] = element
        memberHeads[key] = headsOf(element)
    if not memberHeads:
        return

    memo: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def transitiveHeads(
        key: tuple[str, str], seen: frozenset[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        if key in memo:
            return memo[key]
        if key in seen:
            return set()
        heads: set[tuple[str, str]] = set()
        for head in memberHeads.get(key, ()):
            heads.add(head)
            heads |= transitiveHeads(head, seen | {key})
        memo[key] = heads
        return heads

    declared = {key: declared_type_class(decl) for key, decl in memberDecls.items()}
    reported: set[tuple[str, str]] = set()
    for name, typeKey, particle in entries:
        if name not in memberHeads:
            continue
        if not (transitiveHeads(name, frozenset()) & modelKeys):
            continue
        if typeKey == f"decl:{memberDecls[name].expandedName}":
            continue  # a reference site naming the global member itself
        entryCls = class_of_particle_entry(typeKey, particle)
        memberCls = declared.get(name)
        if entryCls is None or memberCls is None or entryCls is memberCls:
            continue
        if name in reported:
            continue
        reported.add(name)
        local = name[1] or "?"
        ctx.report.add_error(
            f"element declarations consistent: substitution member "
            f"'{local}' is declared with a type conflicting with its "
            "global declaration",
            code="element-consistent",
        )


def class_of_particle_entry(typeKey: str, particle: Any) -> Any:
    """Returns the compiled class a resolved particle entry stands for."""
    if typeKey.startswith("decl:"):
        expanded = typeKey[5:]
        schema = particle.getSchema()
        for element in getattr(schema, "elements", None) or ():
            if getattr(element, "expandedName", None) == expanded:
                return declared_type_class(element)
        return None
    return declared_type_class(particle)


def report_particle_restriction(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports particle-invalid restriction derivations.

    For a complex type whose derivation is a restriction carrying a
    particle (cos-particle-restrict, XSD 1.0 §3.9.6), compiles the
    restricting type's own tree and the base type's effective tree
    and hands them to the pure predicate in
    ``pyxsd.particle_derivation``. Every violation is reported as
    ``particle-restriction`` with the deciding rule name in the
    message.

    Skips — never errors — when the type is not a particle
    restriction, the base type cannot be resolved to a compiled
    type, or either tree could not be compiled (the legacy flat
    fallback); each skip is logged at debug level with its reason.
    A ``RecursionError`` from a pathologically large or nested pair
    is likewise caught and logged, leaving the pair unverified
    rather than crashing the parse.
    """
    if er.getDerivation() != "restriction":
        return
    if not _content_children(er):
        # No particle slot (simple-content restriction, facets only):
        # the particle rules do not apply.
        return
    derived_model = compile_content_model(er, py_xsd)
    if derived_model is None:
        logger.debug(
            "particle restriction: content model of %s could not be compiled; skipped",
            getattr(er, "name", "?"),
        )
        return
    base_class = base_type_class(ctx, er, py_xsd)
    if base_class is None:
        logger.debug(
            "particle restriction: base type %r of %s unresolved; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    base_model = getattr(base_class, "_contentModel_", None)
    if base_model is None:
        logger.debug(
            "particle restriction: base type %s has no compiled content model; skipped",
            getattr(base_class, "name", "?"),
        )
        return
    resolver = lambda particle: getattr(particle, "descriptor", None)  # noqa: E731
    head_lookup = substitution_head_lookup(er, py_xsd)
    member_lookup = substitution_member_lookup(er, py_xsd)
    try:
        reasons = list(
            is_valid_particle_restriction(
                base_model,
                derived_model,
                resolver,
                head_lookup=head_lookup,
                member_lookup=member_lookup,
            )
        )
        reasons.extend(
            derived_wildcard_edc_violations(
                base_model, derived_model, resolver, global_element_lookup(er)
            )
        )
    except RecursionError:
        logger.debug(
            "particle restriction: recursion limit hit checking %s; skipped",
            getattr(er, "name", "?"),
        )
        return
    for reason in reasons:
        ctx.report.add_error(reason, code="particle-restriction")


def report_mixed_restriction(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports a mixed type restricting a non-mixed base type.

    ``Derivation Valid (Restriction, Complex)`` clause 2.4.1 admits a
    mixed derived content type only when the base content type is
    mixed too (2.4.1.2); an element-only derived type may restrict an
    element-only or mixed base (2.4.1.1). The rule bites even when
    the derived particle is empty — a ``maxOccurs=0`` reference makes
    the effective content a synthetic empty sequence, but the content
    type still reads mixed (groupH007v; its non-mixed twin
    groupH008v is valid). A simple-content restriction is not a mixed
    complex-content shape, and an ``xs:anyType`` base is exempt
    (clause 2.1). Skips — never errors — when the base type cannot be
    resolved or is not a complex type definition.
    """
    if er.getDerivation() != "restriction":
        return
    if er._firstProcessedChild(er, "SimpleContent") is not None:
        return
    if not type_is_mixed(er):
        return
    if base_is_any_type(ctx, er, py_xsd):
        return
    base_er = base_type_er(ctx, er, py_xsd)
    if base_er is None or not hasattr(base_er, "effectiveMixed"):
        logger.debug(
            "mixed restriction: base type %r of %s unresolved or not complex; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    if type_is_mixed(base_er):
        return
    ctx.report.add_error(
        "particle restriction (Derivation Valid (Restriction, Complex) "
        "clause 2.4.1): the mixed content type of "
        f"'{getattr(er, 'name', '?')}' cannot restrict the non-mixed "
        f"base type '{getattr(base_er, 'name', '?')}'",
        code="particle-restriction",
    )


def report_mixed_conflict(ctx: CompileContextProtocol, er: Any) -> None:
    """Reports a ``mixed`` conflict between complexType and complexContent.

    XSD 1.1 complex type XML representation: when both the
    ``xs:complexType`` and its ``xs:complexContent`` child carry a
    ``mixed`` attribute, the two values must be identical
    (complex002). An attribute on only one of the two keeps the
    historical reading — the ``complexContent`` value alone decides
    (§3.4.2.3.3 clause 1) — so a one-sided ``mixed`` is not a
    conflict.
    """
    own = (getattr(er, "tagAttributes", {}) or {}).get("mixed")
    if own is None:
        return
    for child in getattr(er, "processedChildren", None) or ():
        if child is not None and type(child).__name__ == "ComplexContent":
            value = (getattr(child, "tagAttributes", {}) or {}).get("mixed")
            if value is not None and _mixedIsTrue(value) != _mixedIsTrue(own):
                ctx.report.add_error(
                    "the mixed attribute of complexType and complexContent "
                    f"must be the same for type '{getattr(er, 'name', '?')}': "
                    f'complexType mixed="{own}" conflicts with '
                    f'complexContent mixed="{value}"',
                    code="declaration-attribute",
                )
            break


def report_mixed_extension(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports an extension whose mixed content diverges from its base.

    Derivation Valid (Extension) clause 1.4.3.2.2 requires the
    {content type} of the derived type to match the base's: a mixed
    type may only extend a mixed base and an element-only type may
    only extend an element-only base. An explicit mixed content over
    an element-only base (complex025) and an element-only content
    that adds particles to a mixed base (complex026) are both errors.
    A bare or attribute-only extension of a mixed base legitimately
    inherits the base content type (§3.4.2.3.3 clause 4.2.2), so it
    is exempt. Hides a simple-content base and an ``xs:anyType``
    base; never errors when the base cannot be resolved or is not a
    complex type definition.
    """
    if er.getDerivation() != "extension":
        return
    if er._firstProcessedChild(er, "SimpleContent") is not None:
        return
    if base_is_any_type(ctx, er, py_xsd):
        return
    base_er = base_type_er(ctx, er, py_xsd)
    if base_er is None or not hasattr(base_er, "effectiveMixed"):
        logger.debug(
            "mixed extension: base type %r of %s unresolved or not complex; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    own = er._ownMixed()
    base = base_er.effectiveMixed()
    if own == base:
        return
    if not own and base and er._explicitContentEmpty():
        return
    ctx.report.add_error(
        "particle restriction (Derivation Valid (Extension) clause "
        f"1.4.3.2.2): the {'mixed' if own else 'element-only'} content "
        f"type of '{getattr(er, 'name', '?')}' cannot be derived by "
        f"extension from the {'mixed' if base else 'element-only'} "
        f"base type '{getattr(base_er, 'name', '?')}'",
        code="particle-restriction",
    )


def report_complex_content_from_simple_base(
    ctx: CompileContextProtocol, er: Any, py_xsd: Any
) -> None:
    """Reports complex content derived from a simple-content base.

    ``Derivation Valid (Extension)`` clause 1.4 and ``Derivation
    Valid (Restriction, Complex)`` clause 2.2 admit a complex-content
    derivation of a complex type only when both sides carry the same
    simple content, both are empty, or the derived content is
    element-only/mixed over an element-only/mixed base. Explicit
    complex content (an empty sequence included) over a base whose
    content type is simple satisfies none of them, so both the
    extension and the restriction shapes are errors under the XSD
    1.1 harness profile (particlesZ031 is the 1.0-valid/1.1-invalid
    split; particlesZ039 is the restriction twin). A base that is
    not a complex type with simple content — ``xs:anyType``, a
    complex base, or an unresolvable reference — is skipped.
    """
    if er.getDerivation() not in ("extension", "restriction"):
        return
    if er._firstProcessedChild(er, "SimpleContent") is not None:
        return
    if er._firstProcessedChild(er, "ComplexContent") is None:
        return
    if base_is_any_type(ctx, er, py_xsd):
        return
    base_er = base_type_er(ctx, er, py_xsd)
    if base_er is None or not hasattr(base_er, "processedChildren"):
        logger.debug(
            "complex content from simple base: base type %r of %s "
            "unresolved or not complex; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    if base_er._firstProcessedChild(base_er, "SimpleContent") is None:
        return
    ctx.report.add_error(
        "particle restriction (Derivation Valid, complex type "
        f"'{getattr(er, 'name', '?')}' adopts explicit complex content "
        f"but its base type '{getattr(base_er, 'name', '?')}' has simple "
        "content)",
        code="particle-restriction",
    )


def report_unique_particle_attribution(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports an ambiguous effective content model (cos-nonambig).

    Unique Particle Attribution is a property of a complex type's
    *effective* content model, so the sweep runs over the compiled
    model — the base type's tree composed with an extension's own
    suffix, which is where an inherited wildcard and a suffix
    wildcard can overlap (particlesZ022). The pure sweep lives in
    :mod:`pyxsd.upa`; it reports the first ambiguity. A type whose
    model could not be compiled, or a model with nothing to compare,
    is skipped.
    """
    if type(er).__name__ != "ComplexType":
        return
    model = compile_content_model(er, py_xsd)
    if model is None:
        logger.debug(
            "unique particle attribution: content model of %s could not be compiled; skipped",
            getattr(er, "name", "?"),
        )
        return
    for reason in upa_violations(model, substitution_head_lookup(er, py_xsd)):
        ctx.report.add_error(reason, code="upa")


def fixed_values_equal(base_attr: Any, derived_attr: Any) -> bool:
    """Whether two attribute uses carry equal ``fixed`` values.

    Returns ``True`` when the base use has no ``fixed``. Lexically
    different spellings that denote one XSD value compare equal, so
    a list attribute's ``fixed`` may be re-spaced and a ``token``
    collapsed (addB183); comparison falls back to the lexical form
    when the attribute's type cannot be resolved.
    """
    base_fixed = base_attr.getFixed()
    if base_fixed is None:
        return True
    derived_fixed = derived_attr.getFixed()
    if derived_fixed is None:
        return False
    if base_fixed == derived_fixed:
        return True
    try:
        base_value = base_attr.getType()(base_fixed)
        derived_value = derived_attr.getType()(derived_fixed)
    except (AttributeError, TypeError, ValueError):
        return False
    return xsd_value_key(base_value) == xsd_value_key(derived_value)


def report_attribute_use_derivation(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports attribute-use derivation violations on a complex type.

    The bounded slice of the attribute clauses the corpus pins:

    * Restriction (XSD 1.0 §3.4.6 clause 2.1.1): a derived use that
      redeclares a *required* base use must stay required;
      weakening it to optional is reported (particlesZ030_d,
      particlesZ017). Omitting the base use entirely is accepted —
      the Saxon 1.1 Assert suite pins such restrictions valid
      (assert011), so the literal XSD 1.0 clause 3 shape is not
      implemented.
    * Extension (XSD 1.1 §3.4.6.2 clause 1.2): a base use must be
      reproduced with identical properties; a redeclaration that
      changes the ``fixed`` value is reported (particlesZ026a).

    The base's effective uses are gathered through its extension
    chain (a restriction does not inherit uses). The check is
    skipped when the base type cannot be resolved or carries an
    XSD 1.1 ``inheritable`` use, which the restriction mapping
    inherits automatically (so omitting it is legal). Attribute
    type derivation and the remaining value-constraint clauses stay
    out of this slice.
    """
    derivation = complex_type_derivation(er)
    if derivation not in ("restriction", "extension"):
        return
    base_er = base_type_er(ctx, er, py_xsd)
    if base_er is None or not hasattr(base_er, "attributes"):
        logger.debug(
            "attribute-use derivation: base type %r of %s unresolved or not complex; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    base_uses, inheritable = effective_attribute_uses(ctx, base_er, py_xsd)
    if not base_uses:
        return
    own_uses = getattr(er, "attributes", None) or {}
    if derivation == "restriction":
        for name, derived_attr in own_uses.items():
            base_attr = base_uses.get(name)
            if base_attr is None:
                continue
            # Clause 2.1.5: a redeclared use keeps the base use's
            # {inheritable} (cta9004err/cta9005err).
            if attribute_use_is_inheritable(base_attr) != attribute_use_is_inheritable(
                derived_attr
            ):
                ctx.report.add_error(
                    f"attribute-use restriction: attribute '{name}' of type "
                    f"'{getattr(er, 'name', '?')}' changes the inheritable "
                    f"({attribute_use_is_inheritable(base_attr)}) of its "
                    f"base type '{getattr(base_er, 'name', '?')}'",
                    code="attribute-restriction",
                )
    if inheritable:
        return
    if derivation == "restriction":
        for name, derived_attr in own_uses.items():
            base_attr = base_uses.get(name)
            if base_attr is None:
                continue
            if attribute_use_is_required(base_attr) and not attribute_use_is_required(derived_attr):
                ctx.report.add_error(
                    f"attribute-use restriction: type "
                    f"'{getattr(er, 'name', '?')}' redeclares the required "
                    f"attribute '{name}' of its base type "
                    f"'{getattr(base_er, 'name', '?')}' as optional",
                    code="attribute-restriction",
                )
            base_fixed = base_attr.getFixed()
            if not fixed_values_equal(base_attr, derived_attr):
                ctx.report.add_error(
                    f"attribute-use restriction: attribute '{name}' of type "
                    f"'{getattr(er, 'name', '?')}' does not preserve the fixed "
                    f"value '{base_fixed}' of its base type "
                    f"'{getattr(base_er, 'name', '?')}'",
                    code="attribute-restriction",
                )
            base_type_cls = declared_type_class(base_attr)
            derived_type_cls = declared_type_class(derived_attr)
            if (
                base_type_cls is not None
                and derived_type_cls is not None
                and derived_type_cls is not base_type_cls
                and not _sameIntegerFamily(derived_type_cls, base_type_cls)
                and is_valid_xsi_type(derived_type_cls, base_type_cls) == "not-derived"
            ):
                # Clause 2.1.2: a redeclared use's type must be validly
                # derived from the base use's (particlesZ013/Z021).
                ctx.report.add_error(
                    f"attribute-use restriction: attribute '{name}' of type "
                    f"'{getattr(er, 'name', '?')}' has a type that is not "
                    f"validly derived from the declared type of its base "
                    f"type '{getattr(base_er, 'name', '?')}'",
                    code="attribute-restriction",
                )
        return
    for name, derived_attr in own_uses.items():
        base_attr = base_uses.get(name)
        if base_attr is None:
            continue
        base_fixed = base_attr.getFixed()
        if not fixed_values_equal(base_attr, derived_attr):
            ctx.report.add_error(
                f"attribute-use extension: attribute '{name}' of type "
                f"'{getattr(er, 'name', '?')}' changes the fixed value "
                f"'{base_fixed}' of its base type "
                f"'{getattr(base_er, 'name', '?')}'",
                code="attribute-restriction",
            )


def effective_attribute_uses(
    ctx: CompileContextProtocol, er: Any, py_xsd: Any, seen: set[int] | None = None
) -> tuple[dict[str, Any], bool]:
    """A type's effective attribute uses, walking its extension chain.

    Returns ``(uses-by-name, has-inheritable)``. A restriction does
    not inherit its base's uses; an extension does. ``seen`` guards
    a derivation cycle (itself an invalid schema).
    """
    uses = dict(getattr(er, "attributes", None) or {})
    has_inheritable = any(attribute_use_is_inheritable(attr) for attr in uses.values())
    if complex_type_derivation(er) != "extension":
        return uses, has_inheritable
    if seen is None:
        seen = set()
    if id(er) in seen:
        return uses, has_inheritable
    seen.add(id(er))
    base_er = base_type_er(ctx, er, py_xsd)
    if base_er is None or not hasattr(base_er, "attributes"):
        return uses, has_inheritable
    base_uses, base_inheritable = effective_attribute_uses(ctx, base_er, py_xsd, seen)
    for name, attr in base_uses.items():
        uses.setdefault(name, attr)
    return uses, has_inheritable or base_inheritable


def complex_type_derivation(er: Any) -> str | None:
    """The type's derivation method, simpleContent wrappers included.

    ``XsdType.getDerivation`` reads ``complexContent`` wrappers only;
    a ``simpleContent`` extension or restriction reports its method
    through this helper.
    """
    derivation = er.getDerivation()
    if derivation is not None:
        return derivation
    simple = er._firstProcessedChild(er, "SimpleContent")
    if simple is None:
        return None
    for child in getattr(simple, "processedChildren", None) or ():
        kind = type(child).__name__
        if kind == "Extension":
            return "extension"
        if kind == "Restriction":
            return "restriction"
    return None


def attribute_use_is_required(attr: Any) -> bool:

    getter = getattr(attr, "getUse", None)
    return callable(getter) and getter() == "required"


def attribute_use_is_inheritable(attr: Any) -> bool:

    attributes = getattr(attr, "tagAttributes", None) or {}
    return str(attributes.get("inheritable") or "").strip().lower() in ("true", "1")


def global_element_lookup(er: Any) -> Any:
    """A lookup from a Clark name to a top-level element declaration.

    Feeds the XSD 1.1 static tighter EDC check (the binding the
    derived type's wildcard resolves to). Names that name no global
    declaration return ``None``, which the check reads as "nothing
    to compare".
    """
    try:
        schema = er.getSchema()
    except AttributeError:
        return lambda name: None
    elements = [
        element
        for element in getattr(schema, "elements", None) or ()
        if type(element).__name__ == "Element" and element.name
    ]

    def lookup(name: str) -> Any:
        local = local_name(name)
        uri = namespace_of(name)
        for element in elements:
            if getattr(element, "name", None) != local:
                continue
            try:
                element_uri = element.getNamespace()
            except AttributeError:
                element_uri = None
            if element_uri == uri:
                return element
        return None

    return lookup


def report_attribute_wildcard_restriction(
    ctx: CompileContextProtocol, er: Any, py_xsd: Any
) -> None:
    """Reports a restriction that does not narrow the base wildcard.

    The derived type's effective attribute wildcard (its local
    ``anyAttribute`` plus the ones its attribute groups contribute,
    intersected) must be a valid restriction of the base type's
    effective wildcard: the namespace constraint may only narrow and
    ``processContents`` must not weaken. A derived type that declares
    no attribute wildcard of its own accepts fewer attributes and is
    always a valid restriction, so only a type whose complete
    wildcard is non-absent is checked. Skips — never errors — when
    the base type cannot be resolved or carries no attribute
    wildcard of its own (an empty derived wildcard admits nothing,
    making the semantic rule depend on more than the constraint
    shape).
    """
    if er.getDerivation() != "restriction":
        return
    baseER = base_type_er(ctx, er, py_xsd)
    if baseER is None:
        logger.debug(
            "attribute wildcard restriction: base type %r of %s unresolved; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    ownSpecs = getattr(er, "wildcardAttributeSpecs", None)
    baseSpecs = getattr(baseER, "wildcardAttributeSpecs", None)
    target = er.getNamespace()
    baseNS = baseER.getNamespace()
    own = effective_attribute_wildcard(ownSpecs, target) if ownSpecs else None
    base = effective_attribute_wildcard(baseSpecs, baseNS) if baseSpecs else None
    # A derived wildcard over a base with no attribute wildcard is
    # never a valid restriction (ctO005): the base admits nothing
    # beyond its own uses.
    if own is not None and base is None:
        ctx.report.add_error(
            f"restriction of type '{er.name}' declares an attribute "
            "wildcard but its base type has none",
            code="wildcard-invalid",
        )
        return
    if base is not None:
        report_restricted_attribute_uses(ctx, er, baseER, base, baseNS, py_xsd)
    if own is None or base is None:
        return
    if not wildcard_subset(own, base, target):
        ctx.report.add_error(
            f"restriction of type '{er.name}' widens the base attribute "
            f"wildcard namespace constraint ('{own.namespace}' is not a "
            f"subset of '{base.namespace}')",
            code="wildcard-invalid",
        )
        return
    if PROCESS_SEVERITY.get(own.process_contents, 2) < PROCESS_SEVERITY.get(
        base.process_contents, 2
    ):
        ctx.report.add_error(
            f"restriction of type '{er.name}' weakens the base attribute "
            f"wildcard processContents ('{own.process_contents}' is weaker "
            f"than '{base.process_contents}')",
            code="wildcard-invalid",
        )


def report_restricted_attribute_uses(
    ctx: CompileContextProtocol,
    er: Any,
    baseER: Any,
    base: Any,
    baseNS: Any,
    py_xsd: Any,
) -> None:
    """Reports derived uses the base's attribute wildcard does not admit.

    Derivation Valid (Restriction, Complex) clause 2: an attribute use
    of the restricting type that does not redeclare a base use must be
    admitted by the base type's {attribute wildcard} (ctO004). Uses
    that share a base use's expanded name are left to the
    attribute-use derivation check.
    """
    base_uses = effective_attribute_uses(ctx, baseER, py_xsd)[0]
    base_keys = {
        attr.instanceName(is_attribute=True, parser=py_xsd) or getattr(attr, "name", None)
        for attr in base_uses.values()
    }
    base_keys.discard(None)
    own_uses = getattr(er, "attributes", None) or {}
    for attr in own_uses.values():
        key = attr.instanceName(is_attribute=True, parser=py_xsd) or getattr(attr, "name", None)
        if key is None or key in base_keys:
            continue
        if not base.allows(namespace_of(key), baseNS):
            ctx.report.add_error(
                f"attribute-use restriction: attribute '{key}' of type "
                f"'{getattr(er, 'name', '?')}' is not admitted by the "
                f"attribute wildcard of its base type "
                f"'{getattr(baseER, 'name', '?')}'",
                code="attribute-restriction",
            )


def base_type_class(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> Any | None:
    """The generated class of the first resolvable base type.

    A probe, not a reporting site: an unresolved base is reported by
    the class build (``unknown-type``), so the lookup stays silent
    here, like the ER-level ``base_type_er`` below.
    """
    for raw_name in getattr(er, "superClassNames", []) or []:
        resolved = er.resolveSchemaQName(raw_name, parser=py_xsd)
        candidate = ElementRepresentative.typeFromName(resolved, py_xsd, warn=False)
        if candidate is not None:
            return candidate
    return None


def base_type_er(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> Any | None:
    """The element representative of the first resolvable base type.

    Mirrors ``ElementRepresentative.typeFromName``'s lookup but
    returns the representative itself, which still carries the XML
    attributes (``mixed``) a generated class does not.
    """
    table = ctx.schema_context.components
    if table is None:
        return None
    strict = getattr(getattr(ctx, "mode", None), "namespaces", "legacy") == "strict"
    for raw_name in getattr(er, "superClassNames", []) or []:
        resolved = er.resolveSchemaQName(raw_name, parser=py_xsd)
        if strict:
            if namespace_of(resolved) == XSD_NS:
                return None
            found = table.getFromName(
                local_name(resolved),
                kind="type",
                namespace=namespace_of(resolved),
                warn=False,
            )
        else:
            found = table.getFromName(resolved, kind="type", warn=False)
            if found is None:
                found = table.getFromName(str(resolved).split(":")[-1], kind="type", warn=False)
        if found is not None:
            return found
    return None


def base_is_any_type(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> bool:
    """Whether the first base type is the built-in ``xs:anyType``.

    ``xs:anyType`` has no generated class, so ``base_type_class``
    cannot resolve it; the extension-structure check reads it as its
    effective mixed-sequence content instead. A user type named
    ``anyType`` never matches (the reference must be in the XML
    Schema namespace or carry the ``xs:``/``xsd:`` spelling in
    legacy mode).
    """
    for raw_name in getattr(er, "superClassNames", []) or []:
        resolved = er.resolveSchemaQName(raw_name, parser=py_xsd)
        if namespace_of(resolved) == XSD_NS and local_name(resolved) == "anyType":
            return True
        if namespace_of(resolved) is not None or not isinstance(raw_name, str):
            continue
        prefix, _, local = raw_name.strip().partition(":")
        if local == "anyType" and prefix in ("xs", "xsd"):
            return True
    return False


def report_extension_structure(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Reports invalid particle composition in a complex type extension.

    An extension's explicit content model is appended to the base
    type's effective content model (XSD 1.1 §3.4.2.3.3). The
    ``all`` compositor is special: an ``all`` may extend only an
    ``all`` (the 1.1 relaxation; ``all`` extends ``sequence``/
    ``choice`` and the reverse are invalid even for singletons —
    all309-312, particlesFb002), the two ``minOccurs`` must match
    (all313), and the composed ``all`` must be unambiguous — no
    repeated element particles (all302) and no overlapping
    wildcards (all305). A base whose *effective* content is empty
    takes the suffix as its model unless the base is mixed, which
    cannot be extended by an ``all`` at all (all308, bug 6202).

    Skips — never errors — when the type is not an extension, when
    it has no explicit content (nothing to append), or when either
    content model cannot be compiled or the base type cannot be
    resolved; each skip is logged at debug level with its reason.
    """
    if er.getDerivation() != "extension":
        return
    own = compile_own_content(er, py_xsd)
    if own is None:
        if _content_children(er):
            logger.debug(
                "extension structure: content model of %s could not be compiled; skipped",
                getattr(er, "name", "?"),
            )
        else:
            logger.debug(
                "extension structure: %s has no explicit content; skipped",
                getattr(er, "name", "?"),
            )
        return
    is_any_type_base = base_is_any_type(ctx, er, py_xsd)
    base_class = None if is_any_type_base else base_type_class(ctx, er, py_xsd)
    if not is_any_type_base and base_class is None:
        logger.debug(
            "extension structure: base type %r of %s unresolved; skipped",
            list(getattr(er, "superClassNames", []) or []),
            getattr(er, "name", "?"),
        )
        return
    base_model = (
        _ANY_TYPE_CONTENT if is_any_type_base else getattr(base_class, "_contentModel_", None)
    )
    if base_model is None:
        logger.debug(
            "extension structure: base type %s has no compiled content model; skipped",
            getattr(base_class, "name", "?"),
        )
        return
    own_term = all_term(own)
    own_kind = "all" if own_term is not None else own.kind
    base_term = all_term(base_model)
    base_kind = "all" if base_term is not None else base_model.kind
    if particle_is_empty(base_model):
        base_er = base_type_er(ctx, er, py_xsd)
        if own_kind == "all" and base_er is not None and type_is_mixed(base_er):
            ctx.report.add_error(
                "particle restriction (cos-ct-extends): an all cannot extend "
                "empty mixed content (the empty mixed base puts the all inside "
                "a sequence)",
                code="particle-restriction",
            )
        return
    if particle_is_empty(own):
        # Empty explicit content appends nothing: the base particle
        # is the effective model, whatever its compositor.
        return
    if own_kind == "all" and base_kind != "all":
        ctx.report.add_error(
            f"particle restriction (cos-ct-extends): all cannot extend {base_kind} "
            "(only an all may extend an all)",
            code="particle-restriction",
        )
        return
    if own_kind != "all" and base_kind == "all":
        ctx.report.add_error(
            f"particle restriction (cos-ct-extends): {own_kind} cannot extend all",
            code="particle-restriction",
        )
        return
    if own_kind != "all" or base_term is None:
        return
    occurrence = all_extension_occurrence(own)
    if occurrence is None:
        return
    own_all, own_min = occurrence
    if own_min != base_model.min_occurs:
        ctx.report.add_error(
            "particle restriction (cos-particle-extend): minOccurs mismatch - "
            "when an all extends an all both must have the same minOccurs "
            f"(base={base_model.min_occurs}, extension={own_min})",
            code="particle-restriction",
        )
    report_extension_all_overlap(ctx, all_members(base_term), all_members(own_all), er, py_xsd)


def report_extension_all_overlap(
    ctx: CompileContextProtocol,
    base_members: list[Any],
    own_members: list[Any],
    er: Any,
    py_xsd: Any,
) -> None:
    """Reports UPA violations in an all-extends-all composition.

    The composed ``all`` holds the base's particles followed by the
    extension's, so a repeated element name (all302) or two
    overlapping wildcards (all305) make it non-deterministic. The
    base's internal and the extension's internal duplicates are
    already reported by the per-compositor sweep; this compares the
    two sides.
    """
    base_names = {
        member.name for member in base_members if member.kind == "element" and member.name
    }
    for member in own_members:
        if member.kind == "element" and member.name and member.name in base_names:
            ctx.report.add_error(
                "particle restriction (cos-nonambig): overlapping particles in "
                f"all extension - element '{member.name}' appears in both the "
                "base and the extension all",
                code="particle-restriction",
            )
            break
    base_wildcards = [member for member in base_members if member.kind == "any" and member.spec]
    own_wildcards = [member for member in own_members if member.kind == "any" and member.spec]
    try:
        target = er.getNamespace()
    except AttributeError:
        target = None
    for first in base_wildcards:
        for second in own_wildcards:
            if wildcard_specs_overlap(first.spec, second.spec, target):
                ctx.report.add_error(
                    "particle restriction (cos-nonambig): overlapping wildcards in all extension",
                    code="particle-restriction",
                )
                return
    # Two element particles from opposite sides can also overlap
    # through a shared substitution member (all303): the composed
    # ``all`` holds both, so a member common to both heads makes it
    # non-deterministic.
    resolved: list[Any] = []
    for member in [*base_members, *own_members]:
        if member.kind != "element":
            continue
        declaration = getattr(member, "descriptor", None)
        if declaration is None or type(declaration).__name__ != "Element":
            continue
        namespace = declaration.getNamespace() or ""
        resolved.append(((namespace, declaration.name or ""), "", declaration))
    if resolved:
        check_substitution_overlap(ctx, resolved, py_xsd)


def particle_is_empty(model: Any) -> bool:
    """Whether a compiled model is an *empty* explicit content model.

    XSD 1.1 §3.4.2.3.3 clause 2: an absent model group, an empty
    ``all``/``sequence``, an empty ``choice`` with ``minOccurs=0``
    and any model group with ``maxOccurs=0`` are empty content. (An
    empty ``choice`` with ``minOccurs=1`` is unsatisfiable, not
    empty.)
    """
    if model.max_occurs == 0:
        return True
    if model.kind in ("sequence", "all") and not model.children:
        return True
    return model.kind == "choice" and not model.children and model.min_occurs == 0


def type_is_mixed(er: Any) -> bool:
    """Whether a complex type's effective ``mixed`` value is true.

    Delegates to the representative's own XSD 1.1 §3.4.2.3.3
    clause 1 reading; a representative without the method (a
    built-in, say) is not mixed.
    """
    mixed_method = getattr(er, "effectiveMixed", None)
    return bool(mixed_method()) if mixed_method is not None else False


def substitution_head_lookup(er: Any, py_xsd: Any) -> Callable[[Any], Any] | None:
    """A declaration-to-head resolver for NameAndTypeOK.

    NameAndTypeOK admits a restricting element that is a (transitive)
    member of the base element's substitution group; the walk needs
    each member's head *declaration*, which only the schema's global
    element table can supply. Under XSD 1.1 a local declaration with
    no ``substitutionGroup`` of its own shares the membership of a
    global declaration with the same expanded name (all226; XSD 1.1
    bug 5296), so the lookup falls back to such a global before
    giving up. ``None`` when this schema has no element table, in
    which case the predicate falls back to exact expanded-name
    equality.
    """
    try:
        schema = er.getSchema()
    except AttributeError:
        return None
    if schema is None:
        return None
    candidates = [
        element
        for element in getattr(schema, "elements", None) or []
        if type(element).__name__ == "Element"
    ]

    def lookup(declaration: Any) -> Any:
        head_name = declaration.getSubstitutionGroupHead(py_xsd)
        resolving = declaration
        if not head_name:
            # XSD 1.1: a local declaration whose expanded name is
            # also declared globally inherits that global's
            # substitution-group membership. The expanded names are
            # form-aware: an unqualified local is *not* the same
            # expanded name as a same-named global (elemZ020).
            for candidate in candidates:
                if candidate is declaration:
                    continue
                if candidate.name == declaration.name and element_namespace(
                    candidate
                ) == element_namespace(declaration):
                    head_name = candidate.getSubstitutionGroupHead(py_xsd)
                    resolving = candidate
                    break
        if not head_name:
            return None
        resolver = getattr(resolving, "resolveReference", None)
        if resolver is None:
            return None
        return resolver(head_name, candidates, parser=py_xsd)

    return lookup


def substitution_member_lookup(er: Any, py_xsd: Any) -> Callable[[Any], list[Any]] | None:
    """A declaration-to-members resolver for the clause 2.1 expansion.

    §3.9.6 clause 2.1 treats a substitution-group head's element
    particle as a choice group with one particle per member, so the
    particle-derivation predicate needs the reverse of
    ``substitution_head_lookup``: the declarations that name this
    head. ``None`` when the schema's element table is unavailable,
    in which case the predicate leaves the head unexpanded.
    """
    head_lookup = substitution_head_lookup(er, py_xsd)
    if head_lookup is None:
        return None
    try:
        schema = er.getSchema()
    except AttributeError:
        return None
    if schema is None:
        return None
    candidates = [
        element
        for element in getattr(schema, "elements", None) or []
        if type(element).__name__ == "Element"
    ]

    def members(declaration: Any) -> list[Any]:
        try:
            block_raw = declaration.getBlock()
        except AttributeError:
            # An unresolved reference site has no referred declaration
            # to read the ``block`` from.
            block_raw = declaration.tagAttributes.get("block")
        block_tokens = str(block_raw or "").split()
        if "substitution" in block_tokens or "#all" in block_tokens:
            # A head that blocks substitution has no admissible members,
            # so the clause 2.1 head expansion contributes none
            # (elemZ027_b).
            return []
        found = []
        for element in candidates:
            if element is declaration:
                continue
            head = head_lookup(element)
            if head is not None and head is declaration:
                found.append(element)
        return found

    return members


def report_pointless_particle(ctx: CompileContextProtocol, er: Any) -> None:
    """Reports a pointless ``sequence``/``choice`` inside an optional group.

    The particlesHa rule: a compositor with *no particle children*
    (``{particles}`` is empty) whose ``minOccurs`` lets it match
    zero — the ERs' ``emptiable`` property — is pointless when the
    group definition containing it is referenced with
    ``minOccurs="0"``, because the optional reference can always be
    satisfied without it. Such a particle must be eliminated
    (particlesHa008). Deliberately conservative shapes beyond that
    are not reported: a compositor with children can still match
    content even when every child is optional or ``maxOccurs="0"``,
    so eliminating it could change the content model's language
    (MS pins such schemas valid — groupL007 — and real-world
    schemas, ECMA-376 among them, use the shape heavily), and an
    empty compositor with ``minOccurs="1"`` is unsatisfiable rather
    than eliminable. ``all`` compositors are not reported — their
    legality is the ``all-rule``.
    """
    if er._particleChildren() or not getattr(er, "emptiable", False):
        return
    definition = containing_group_definition(er)
    if definition is None:
        return
    for refSite in group_ref_sites(definition):
        if is_optional_particle(refSite):
            ctx.report.add_error(
                f"pointless particle: the empty {er.rawTag} in group "
                f"'{definition.name}' matches no elements and the "
                "group reference is optional (minOccurs=0); the "
                "particle is pointless and must be eliminated",
                code="pointless-particle",
            )
            return


def containing_group_definition(er: Any) -> Any | None:
    """The group definition a compositor's occurrence context hangs on.

    Walks up through enclosing compositors (their own occurrence
    does not make an emptiable child required) and returns the
    group definition the chain ends in, or ``None`` for any other
    container (a complex type root, say) or a malformed chain.
    """
    node = er.parent
    while node is not None:
        kind = type(node).__name__
        if kind in _COMPOSITOR_KINDS:
            node = node.parent
            continue
        if kind == "Group" and not getattr(node, "isRefSite", False):
            return node
        return None
    return None


def group_ref_sites(definition: Any) -> list[Any]:
    """The group reference sites in this schema that name *definition*.

    The whole document tree is scanned (definitions do not record
    who references them); a reference that cannot be resolved to
    this definition — including one naming an import — is skipped.
    """
    try:
        schema = definition.getSchema()
    except AttributeError:
        return []
    refs: list[Any] = []
    stack = [schema] if schema is not None else []
    seen: set[int] = set()
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "Group" and getattr(er, "isRefSite", False):
            try:
                if definition.resolveGroupRef(er) is definition:
                    refs.append(er)
            except NamespaceError:
                continue
        stack.extend(getattr(er, "processedChildren", None) or ())
    return refs


def is_optional_particle(particle: Any) -> bool:
    """Whether a particle's raw ``minOccurs`` is 0 (silent read)."""
    return getattr(particle, "minOccurs", None) == "0"


def global_elements(er: Any) -> list[Any]:
    """The schema document's global element declarations."""
    try:
        schema = er.getSchema()
    except AttributeError:
        return []
    return [
        element
        for element in getattr(schema, "elements", None) or ()
        if type(element).__name__ == "Element" and element.name
    ]


def resolve_particles(particles: list[Any]) -> list[tuple[tuple[str, str], str, Any]]:
    """Maps particles to ``(expanded name, type key, particle)``.

    A reference site adopts the referred declaration's expanded name
    and a shared ``decl:`` type key; a reference that does not
    resolve against this document's globals is dropped (its type is
    unknown). A name site keeps its declared name and the raw (or
    inline-generated) ``type`` attribute as its key.
    """
    globals_ = global_elements(particles[0] if particles else None)
    resolved: list[tuple[tuple[str, str], str, Any]] = []
    for particle in particles:
        if getattr(particle, "isElementRef", False):
            referred = None
            resolver = getattr(particle, "resolveReference", None)
            if resolver is not None:
                referred = resolver(
                    getattr(particle, "ref", None),
                    globals_,
                    parser=getattr(particle.getSchema(), "host", None),
                )
            if referred is None:
                continue
            try:
                namespace = referred.getNamespace() or ""
            except AttributeError:
                namespace = ""
            name = (namespace, referred.name or "")
            resolved.append((name, f"decl:{referred.expandedName}", particle))
            continue
        try:
            namespace = particle.getNamespace() or ""
        except AttributeError:
            namespace = ""
        name = (namespace, getattr(particle, "name", None) or "")
        typeKey = particle.tagAttributes.get("type") or ""
        resolved.append((name, typeKey, particle))
    return resolved


def check_conditional_type_substitutable(
    ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any
) -> None:
    """Reports a restriction whose alternative types are not substitutable.

    XSD 1.1 cos-cta-substitutable (§3.4.6.4): for a restriction, each
    type in the derived type's type table at a given test must be
    validly derived from the base's type at the same test, and the two
    tables must carry the same tests. Runs after the generated classes
    exist so alternative types have resolved classes (cta0043).
    """
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "ComplexType" and er.getDerivation() == "restriction":
            check_one_conditional_type(ctx, er, py_xsd)
        stack.extend(getattr(er, "processedChildren", None) or ())


def check_one_conditional_type(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:

    if er._firstProcessedChild(er, "SimpleContent") is not None:
        return
    base_class = base_type_class(ctx, er, py_xsd)
    if base_class is None:
        return
    base_model = getattr(base_class, "_contentModel_", None)
    derived_model = getattr(getattr(er, "_generatedClass", None), "_contentModel_", None)
    if derived_model is None:
        derived_model = compile_content_model(er, py_xsd)
    if base_model is None or derived_model is None:
        return
    base_decls = model_element_declarations(base_model)
    derived_decls = model_element_declarations(derived_model)
    for name, base_decl in base_decls.items():
        derived_decl = derived_decls.get(name)
        if derived_decl is None:
            continue
        reason = alternative_substitutability_violation(base_decl, derived_decl, py_xsd)
        if reason is not None:
            ctx.report.add_error(
                f"particle restriction (cos-cta-substitutable): the type "
                f"alternatives of element '{name[1] or '?'}' in type "
                f"'{getattr(er, 'name', '?')}' are not substitutable for the "
                f"base type's ({reason})",
                code="particle-restriction",
                phase="schema",
            )


def model_element_declarations(model: Any) -> dict[tuple[str, str], Any]:
    """Maps each element particle's expanded name to its declaration."""
    found: dict[tuple[str, str], Any] = {}

    def walk(particle: Any) -> None:
        if particle.kind == "element":
            declaration = getattr(particle, "descriptor", None)
            if declaration is not None and getattr(declaration, "name", None):
                namespace = element_namespace(declaration) or ""
                found.setdefault((namespace, declaration.name), declaration)
        for child in particle.children:
            walk(child)

    walk(model)
    return found


def alternative_substitutability_violation(
    base_decl: Any, derived_decl: Any, py_xsd: Any
) -> str | None:

    base_alts = getattr(base_decl, "compiledAlternatives", None) or []
    derived_alts = getattr(derived_decl, "compiledAlternatives", None) or []
    if not base_alts and not derived_alts:
        return None
    for alt in (*base_alts, *derived_alts):
        # The declarations may be untyped (their alternatives were
        # never resolved by ``check_element_alternatives``), so resolve
        # each alternative's type class here.
        alt.er.resolveTypeClass(py_xsd)
    base_table = {alt.test: alt for alt in base_alts}
    derived_table = {alt.test: alt for alt in derived_alts}
    if set(base_table) != set(derived_table):
        return "the type tables carry different tests"
    for test, base_alt in base_table.items():
        derived_alt = derived_table[test]
        if base_alt.is_error or derived_alt.is_error:
            continue
        base_cls = base_alt.type_class
        derived_cls = derived_alt.type_class
        if base_cls is None or derived_cls is None:
            continue
        if is_validly_derived(derived_cls, base_cls) is not None:
            return f"the type at test {test!r} is not validly derived from the base's"
    return None


def check_alternative_table_edc(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Reports like-named particles whose ``xs:alternative`` tables differ.

    Element Declarations Consistent compares the *type table* of
    same-named element particles as well as their declared types
    (bug 11076): two ``xs:alternative`` lists that differ — including
    one absent — assign conflicting governing types
    (cta9009err/cta9010err). Runs after the declaration walk (and
    after ``check_alternatives``) because alternatives are compiled on
    each element during that walk.
    """
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ in _COMPOSITOR_KINDS:
            raw: list[Any] = []
            collect_particles(er, raw, set())
            resolved = resolve_particles(raw)
            byName: dict[tuple[str, str], list[Any]] = {}
            for name, _typeKey, particle in resolved:
                byName.setdefault(name, []).append(particle)
            for (_, local), particles in byName.items():
                if len(particles) < 2:
                    continue
                if len({alternative_table_signature(p) for p in particles}) > 1:
                    ctx.report.add_error(
                        f"element declarations consistent: element '{local}' "
                        "is declared with conflicting alternative type tables "
                        "in the same content model",
                        code="all-rule",
                    )
            check_wildcard_element_edc(ctx, er, resolved)
        stack.extend(getattr(er, "processedChildren", None) or ())


def alternative_type_key(alternative: Any) -> tuple:
    """A canonical identity for one alternative's type.

    A named type compares by its (string) name. An inline type has no
    name and its element representative is a distinct object per
    declaration, so it is canonicalized by its serialized XML
    structure: structurally identical inline types compare equal even
    though their ERs differ.
    """
    if alternative.type_name is not None:
        return ("name", alternative.type_name)
    inline = alternative.inline_type
    if inline is None:
        return ("none", None)
    element = getattr(inline, "xsdElement", None)
    if element is not None:
        try:
            return ("inline", ET.tostring(element))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
    return ("inline", id(inline))


def alternative_table_signature(particle: Any) -> tuple:
    """An element particle's ``xs:alternative`` type table signature.

    Returns a tuple of ``(test, type)`` pairs in declaration order, or
    an empty tuple when the declaration carries no alternatives. A
    reference site follows its ``referredElement`` to the declaration
    that owns the alternatives.
    """
    seen: set[int] = set()
    current = particle
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        alternatives = getattr(current, "compiledAlternatives", None)
        if alternatives:
            return tuple((alt.test, alternative_type_key(alt)) for alt in alternatives)
        current = getattr(current, "referredElement", None)
    return ()


def collect_particles(er: Any, out: list[Any], visited: set[int]) -> None:
    """Collects element particles under *er* transitively.

    Descends nested compositors and — because a group reference's
    content model is spliced into the referencing type's model —
    through group references to their definition's compositor
    (mgR022's conflicting ``e1`` sits inside a referenced sequence).
    A reference that cannot be resolved here (it names an import)
    is skipped: its contents belong to that document's own sweep.
    """
    for child in getattr(er, "processedChildren", None) or ():
        if child is None or id(child) in visited:
            continue
        visited.add(id(child))
        kind = type(child).__name__
        if kind == "Element":
            out.append(child)
        elif kind in _COMPOSITOR_KINDS:
            collect_particles(child, out, visited)
        elif kind == "Group" and getattr(child, "isRefSite", False):
            definition = group_ref_definition(child)
            if definition is None:
                continue
            compositor = definition.getCompositor()
            if compositor is not None and id(compositor) not in visited:
                visited.add(id(compositor))
                collect_particles(compositor, out, visited)


def group_ref_definition(refSite: Any) -> Any | None:
    """The group definition a reference site names, or ``None``.

    Mirrors the content-model compiler's resolution: QName-aware
    where the document supplies a namespace context, then the
    schema's group table by full reference and local name.
    """
    ref = getattr(refSite, "ref", None)
    if not ref:
        return None
    try:
        schema = refSite.getSchema()
    except AttributeError:
        return None
    groups = getattr(schema, "groups", None)
    if not groups:
        return None
    resolver = getattr(refSite, "resolveReference", None)
    if resolver is not None:
        resolved = resolver(ref, groups.values(), parser=getattr(schema, "host", None))
        if resolved is not None:
            return resolved
    return groups.get(ref) or groups.get(ref.split(":")[-1])


def check_substitution_overlap(
    ctx: CompileContextProtocol,
    resolved: list[tuple[tuple[str, str], str, Any]],
    py_xsd: Any,
) -> None:
    """Reports a substitution-related pair meeting inside one ``all``.

    A member can stand wherever its head appears, so two particles
    under one ``all`` are ambiguous when their expanded name sets —
    the particle's own name plus every declaration that may
    substitute for it — intersect. That covers the head/member pair
    (all241), a member shared by two heads (all242: ``q`` substitutes
    for both ``o`` and ``p``), and a member belonging to two groups
    whose heads are both present (subsgroup903). A head whose
    {block} excludes substitution cannot be substituted here, so it
    does not expand and the model stays deterministic (elemZ028a).
    Only the immediate substitution step is consulted; transitive
    chains are left to the substitution-group machinery.
    """
    if not resolved:
        return
    try:
        schema = resolved[0][2].getSchema()
    except AttributeError:
        return
    memberHeads: dict[tuple[str, str], set[tuple[str, str]]] = {}
    blockedHeads: set[tuple[str, str]] = set()

    def headsOf(holder: Any) -> set[tuple[str, str]]:
        names: set[tuple[str, str]] = set()
        for head in holder.getSubstitutionGroupHeads(py_xsd):
            if head.startswith("{"):
                namespace, _, local = head[1:].partition("}")
                names.add((namespace, local))
            else:
                names.add((element_namespace(holder) or "", head.split(":")[-1]))
        return names

    def nameOf(holder: Any) -> tuple[str, str]:
        return (element_namespace(holder) or "", getattr(holder, "name", None) or "")

    # Form-aware names: an unqualified local declaration never
    # collides with a same-spelled global (elemZ020), and a
    # reference site adopts its global's expanded name.
    entries: list[tuple[tuple[str, str], Any]] = []
    for name, _typeKey, particle in resolved:
        if getattr(particle, "isElementRef", False):
            entries.append((name, particle))
        else:
            entries.append((nameOf(particle), particle))
    for element in getattr(schema, "elements", None) or ():
        if type(element).__name__ != "Element" or not element.name:
            continue
        block = (element.tagAttributes.get("block") or "").split()
        if "substitution" in block or "#all" in block:
            blockedHeads.add(nameOf(element))
        if element.tagAttributes.get("substitutionGroup"):
            memberHeads.setdefault(nameOf(element), set()).update(headsOf(element))
    for name, particle in entries:
        block = (particle.tagAttributes.get("block") or "").split()
        if "substitution" in block or "#all" in block:
            blockedHeads.add(name)
        if particle.tagAttributes.get("substitutionGroup"):
            memberHeads.setdefault(name, set()).update(headsOf(particle))

    expanded: list[tuple[tuple[str, str], set[tuple[str, str]]]] = []
    for name in {name for name, _particle in entries}:
        names = {name}
        if name not in blockedHeads:
            for member, heads in memberHeads.items():
                if name in heads:
                    names.add(member)
        expanded.append((name, names))
    for index, (first, firstNames) in enumerate(expanded):
        for second, secondNames in expanded[index + 1 :]:
            if firstNames & secondNames:
                firstLabel = first[1] if first[1] else "?"
                secondLabel = second[1] if second[1] else "?"
                ctx.report.add_error(
                    f"content model is ambiguous: elements '{firstLabel}' and "
                    f"'{secondLabel}' can match the same substitution member "
                    "in the same content model",
                    code="all-rule",
                )
                return


def check_all_wildcard_overlap(ctx: CompileContextProtocol, er: Any) -> None:
    """Reports two overlapping wildcards under one all (all243).

    Wildcards carrying XSD 1.1 ``notNamespace``/``notQName``
    constraints are skipped: their exclusion sets can make
    namespace-overlapping wildcards disjoint (wild049), and the
    1.1 wildcard algebra is out of scope here.
    """
    wildcards = [
        child
        for child in getattr(er, "processedChildren", None) or ()
        if child is not None
        and child.__class__.__name__ == "Any"
        and child.xsdElement.get("notNamespace") is None
        and child.xsdElement.get("notQName") is None
    ]
    try:
        target = er.getNamespace()
    except AttributeError:
        target = None
    for index, first in enumerate(wildcards):
        for second in wildcards[index + 1 :]:
            if wildcard_specs_overlap(first.wildcardSpec, second.wildcardSpec, target):
                ctx.report.add_error(
                    "content model is ambiguous: an all contains two overlapping wildcards",
                    code="all-rule",
                )
                return


def check_wildcard_particle_overlap(ctx: CompileContextProtocol, er: Any) -> None:
    """Reports non-deterministic wildcard pairs in a sequence/choice.

    Two element wildcards in one content model whose namespace
    constraints overlap violate Unique Particle Attribution when both
    can match an item at the same point: in a ``choice`` every
    alternative is live at once, while in a ``sequence`` the earlier
    wildcard creates the ambiguity only when it can match again
    (``maxOccurs`` > 1) or be skipped (``minOccurs`` = 0) while every
    particle between the two is emptiable (wildI013/I014 are
    non-deterministic; the single-occurrence wildI011 and the
    second-repeats wildI012 are deterministic and stay valid).
    Wildcards carrying XSD 1.1 ``notNamespace``/``notQName`` are
    skipped (their exclusion sets are a separate task), as is a
    malformed namespace constraint — the declaration check already
    reported that token.
    """
    children = [
        child
        for child in er._particleChildren()
        if not (
            child.__class__.__name__ == "Any"
            and (
                child.xsdElement.get("notNamespace") is not None
                or child.xsdElement.get("notQName") is not None
            )
        )
    ]
    wildcards = [
        (index, child) for index, child in enumerate(children) if child.__class__.__name__ == "Any"
    ]
    if len(wildcards) < 2:
        return
    if unreferenced_group_content(er):
        return
    try:
        target = er.getNamespace()
    except AttributeError:
        target = None
    isChoice = type(er).__name__ == "Choice"
    for position, (index, first) in enumerate(wildcards):
        if not isChoice and not wildcard_matchable_again(first):
            continue
        for laterIndex, second in wildcards[position + 1 :]:
            if not isChoice and not all(
                getattr(child, "emptiable", False) for child in children[index + 1 : laterIndex]
            ):
                continue
            firstSpec = first.wildcardSpec
            secondSpec = second.wildcardSpec
            if invalid_namespace_constraint(firstSpec.namespace) is not None:
                continue
            if invalid_namespace_constraint(secondSpec.namespace) is not None:
                continue
            if wildcard_specs_overlap(firstSpec, secondSpec, target):
                ctx.report.add_error(
                    "content model is ambiguous: two wildcards with "
                    "overlapping namespace constraints are not deterministic",
                    code="wildcard-invalid",
                )
                return


def wildcard_matchable_again(particle: Any) -> bool:
    """Whether a sequence wildcard is still live when the next is.

    A wildcard that can repeat (``maxOccurs`` > 1) is live again
    after one match, and a skippable one (``minOccurs`` = 0) is live
    next to the following particle from the start. The silent reads
    avoid duplicating the declaration walk's ``invalid-occurs``
    report for a garbage occurrence value.
    """
    return particle._silentOccurs("maxOccurs") > 1 or particle._silentOccurs("minOccurs") == 0


def unreferenced_group_content(er: Any) -> bool:
    """Whether *er*'s content model belongs to an unreferenced group.

    Unique Particle Attribution applies to the effective content
    model of a complex type, not to an orphan group definition: the
    corpus pins an ambiguous wildcard sequence inside a group that
    nothing references as *valid* (addB194), and the oracle accepts
    it. A group a complex type's model actually reaches — directly
    or through a chain of group references — becomes part of that
    model, so its compositors are still checked. A reference that
    cannot be resolved here counts as no reference (its contents
    belong to another document's own sweep).
    """
    container = er.getContainingType()
    if container is None or type(container).__name__ != "Group":
        return False
    return id(container) not in referenced_group_ids(er)


def referenced_group_ids(er: Any) -> set[int]:
    """The ids of group definitions a complex type's model can reach.

    UPA is a property of a complex type definition's content model,
    so only group definitions on a path from some complex type are
    swept. The seed set is every complex type declaration in the
    document; the walk then follows each ``group`` reference from
    the referencing model into the definition it names, so a chain
    of referenced groups is covered while a group named only by
    another unreferenced group stays an orphan (I1: the reference
    relation is not one-hop).
    """
    try:
        schema = er.getSchema()
    except AttributeError:
        return set()
    global _referenced_group_id_cache
    cached = _referenced_group_id_cache
    if cached is not None:
        cached_schema = cached[0]()
        if cached_schema is schema:
            return cached[1]
        # A dead or different schema ER: drop the slot so a new schema
        # can claim it.
        _referenced_group_id_cache = None
    types: list[Any] = []
    seen: set[int] = set()
    stack = [schema]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if type(node).__name__ == "ComplexType":
            types.append(node)
        stack.extend(getattr(node, "processedChildren", None) or ())
    referenced: set[int] = set()
    walked: set[int] = set()
    for complexType in types:
        stack = [complexType]
        while stack:
            node = stack.pop()
            if node is None or id(node) in walked:
                continue
            walked.add(id(node))
            if type(node).__name__ == "Group" and getattr(node, "isRefSite", False):
                definition = group_ref_definition(node)
                if definition is not None:
                    referenced.add(id(definition))
                    stack.append(definition)
                continue
            stack.extend(getattr(node, "processedChildren", None) or ())
    with contextlib.suppress(TypeError):  # non-weakrefable representative
        _referenced_group_id_cache = (weakref.ref(schema), referenced)
    return referenced


def check_declaration_id(ctx: CompileContextProtocol, er: Any, seenIds: dict[str, Any]) -> None:
    """Reports lexical/duplicate ``id`` attributes on declarations.

    Every ``id`` on a schema component is an ``xs:ID``: it must be a
    valid NCName and unique within its schema document. Uniqueness is
    scoped to the main document because "document" is the unit of the
    XML ID rule; components spliced in from includes/imports are not
    compared against it (``_composedElementIds``). The walk visits
    every representative once, so uniqueness is tracked here rather
    than on each subclass.
    """
    raw = getattr(er, "id", None)
    if raw is None:
        return
    # ``id`` is an ``xs:ID`` (an ``xs:NCName`` with whiteSpace=collapse),
    # so compare the collapsed value: two ids differing only in
    # surrounding whitespace are the same XML ID.
    value = str(raw).strip()
    try:
        NCName(value)
    except TypeError:
        ctx.report.add_error(
            f"id '{value}' on <{er.rawTag}> is not a valid NCName",
            code="declaration-attribute",
            element=er.rawTag,
            phase="schema",
        )
        return
    element = getattr(er, "xsdElement", None)
    if element is not None and id(element) in ctx.composed_element_ids:
        # A component from an included/imported document: ``id``
        # uniqueness is scoped to a single schema document, so it is
        # not compared against the main document's ids.
        return
    if value in seenIds:
        ctx.report.add_error(
            f"duplicate id '{value}' on <{er.rawTag}>",
            code="declaration-duplicate",
            element=er.rawTag,
            phase="schema",
        )
        return
    seenIds[value] = er


def check_duplicate_name(
    ctx: CompileContextProtocol, er: Any, declared: dict[tuple[str, Any, str], Any]
) -> None:
    """Reports a named component declared twice in one symbol space.

    XSD 1.0 §2.5 gives each target namespace one symbol space per
    global component kind, except that simple and complex type
    definitions share one; element, attribute, model group and
    attribute group definitions each have their own. A second
    declaration of the same name in a symbol space is the reported
    error. Identity-constraint names (``key``/``keyref``/
    ``unique``) share one symbol space scoped to their containing
    element declaration. Local element and attribute declarations
    are scoped to their containing complex type and are not
    compared here.
    """
    element = getattr(er, "xsdElement", None)
    name = element.get("name") if element is not None else None
    if not name:
        return
    typeName = type(er).__name__
    isIdentity = typeName in _IDENTITY_KINDS
    if typeName == "Notation":
        # A notation definition occupies the notation symbol space of
        # its target namespace (notatB005).
        key = ("notation", er.getNamespace(), name)
        if key in declared:
            ctx.report.add_error(
                f"duplicate notation declaration '{name}'",
                code="declaration-duplicate",
                element=er.rawTag,
                phase="schema",
            )
            return
        declared[key] = er
        return
    if element is not None and id(element) in ctx.composed_element_ids and not isIdentity:
        # A component spliced in from an included, imported or
        # redefined document is not compared against the main
        # schema's components. Composition may legitimately expose a
        # name twice (nested redefines, a re-parsed document), so the
        # check is scoped to the main document, matching the ``id``
        # uniqueness scope in ``check_declaration_id``. Identity
        # constraints are compared across composed documents because
        # their names share the target namespace's symbol space
        # (targetNS00101m2).
        return
    if in_conditional_inclusion(er):
        # XSD 1.1 conditional-inclusion declarations (``vc:*``) may
        # share a name, selected by version or availability. Without
        # evaluating the selectors, do not report the duplicate.
        return
    if isIdentity:
        parent = getattr(er, "parent", None)
        if parent is None or type(parent).__name__ != "Element":
            return
        # Identity-constraint names are unique per target namespace
        # (not per containing element): the same key name on two
        # elements of one namespace is a schema error, while the same
        # name in another namespace is not (targetNS00101m1/m2).
        key = ("identity", er.getNamespace(), name)
        label = "identity constraint"
    else:
        kind = componentKind(er)
        if kind is None or not er.isGlobalDeclaration():
            return
        namespace = er.getNamespace()
        if namespace in (XML_NS, XLINK_NS):
            # The XML-namespace (xml:lang, xml:space, ...) and XLink
            # attribute declarations are registered as built-ins
            # before the ER run and may also be reached from the
            # namespace's own schema; a repeat there is not an
            # authoring error.
            return
        key = (kind, namespace, name)
        label = {
            "element": "element",
            "attribute": "attribute",
            "type": "type",
            "group": "group",
            "attributeGroup": "attributeGroup",
        }[kind]
    if key in declared:
        ctx.report.add_error(
            f"duplicate {label} declaration '{name}'",
            code="declaration-duplicate",
            element=er.rawTag,
            phase="schema",
        )
        return
    declared[key] = er


def in_conditional_inclusion(er: Any) -> bool:
    """Whether *er* or an ancestor carries an ``vc:*`` attribute.

    Conditional inclusion can produce declarations that do not
    coexist for any single schema version, so their names must not be
    compared without evaluating the version selectors.
    """
    node = er
    while node is not None:
        element = getattr(node, "xsdElement", None)
        if element is not None and any(key.startswith(f"{{{VC_NS}}}") for key in element.attrib):
            return True
        node = getattr(node, "parent", None)
    return False


def check_schema_namespaced_attributes(ctx: CompileContextProtocol, er: Any) -> None:
    """Reports an XML-Schema-namespace attribute on a schema element.

    Schema components carry unqualified attributes; a qualified
    attribute in the XML Schema namespace (``xsd:targetNamespace``
    on ``schema``, ``xsd:type`` on an ``attribute``) is never a legal
    attribute of a schema element (addB070a, addB082, notatE002).
    Attributes from a genuinely foreign namespace are not examined.
    ``documentation`` bodies are arbitrary XML and are skipped.
    """
    element = getattr(er, "xsdElement", None)
    if element is None or namespace_of(element.tag) != XSD_NS:
        return
    if type(er).__name__ == "Documentation":
        return
    prefix = f"{{{XSD_NS}}}"
    for attr in element.attrib:
        if not attr.startswith(prefix):
            continue
        local = attr[len(prefix) :]
        ctx.report.add_error(
            f"the attribute '{local}' is in the XML Schema namespace and is "
            f"not a legal attribute of <{er.rawTag}>",
            code="unexpected-attribute",
            element=er.name if isinstance(er.name, str) else None,
            phase="schema",
        )


def check_child_grammar(ctx: CompileContextProtocol, er: Any) -> None:
    """Reports a declaration's children against its grammar table.

    The table lives on the element representative: ``_ALLOWED_CHILDREN``
    (illegal children), ``_MAX_ONE_CHILDREN`` (duplicates),
    ``_CHILD_ORDER``/``_EXCLUSIVE_SLOTS`` (ordering). A class without
    an ``_ALLOWED_CHILDREN`` table is not checked for illegal
    children; its duplicate/order tables default to empty unless it
    declares otherwise.
    """
    if er._ALLOWED_CHILDREN is not None:
        for tag in er.unexpectedChildTags:
            ctx.report.add_error(
                f"<{tag}> is not allowed inside <{er.rawTag}>",
                code="declaration-child",
                element=er.rawTag,
                phase="schema",
            )
    counts = Counter(er.childTags)
    for tag in er._MAX_ONE_CHILDREN:
        if counts[tag] > 1:
            ctx.report.add_error(
                f"<{er.rawTag}> may contain at most one <{tag}>",
                code="declaration-duplicate",
                element=er.rawTag,
                phase="schema",
            )
    # Every declaration requires a present ``annotation`` to be its
    # first schema child; only the schema root is exempt (it allows
    # annotations before and after declarations). ``childTags`` holds
    # schema-namespace children only, so a foreign element named
    # ``annotation`` never triggers this.
    if er._ANNOTATION_FIRST and "annotation" in counts and er.childTags.index("annotation") != 0:
        ctx.report.add_error(
            f"<annotation> must be the first child of <{er.rawTag}>",
            code="declaration-order",
            element=er.rawTag,
            phase="schema",
        )
    if er._CHILD_ORDER:
        slot_of = {tag: i for i, slot in enumerate(er._CHILD_ORDER) for tag in slot}
        seen = [slot_of[t] for t in er.childTags if t != "annotation" and t in slot_of]
        if seen != sorted(seen):
            ctx.report.add_error(
                f"children of <{er.rawTag}> are out of order",
                code="declaration-order",
                element=er.rawTag,
                phase="schema",
            )
        occupied = set(seen)
        for slot in er._EXCLUSIVE_SLOTS:
            if slot in occupied and any(x > slot for x in occupied):
                ctx.report.add_error(
                    f"<{er.rawTag}> with this content kind cannot also contain later declarations",
                    code="declaration-order",
                    element=er.rawTag,
                    phase="schema",
                )
        # A "one of" slot is an alternative: two *distinct* tags in the
        # same slot are illegal (``choice``+``group``), even though the
        # per-tag max-one check above sees each only once.
        for slot in er._ONE_OF_SLOTS:
            slotTags = {t for t in er.childTags if t != "annotation" and slot_of.get(t) == slot}
            if len(slotTags) > 1:
                choices = ", ".join(f"<{tag}>" for tag in sorted(slotTags))
                ctx.report.add_error(
                    f"<{er.rawTag}> may contain only one of {choices}",
                    code="declaration-duplicate",
                    element=er.rawTag,
                    phase="schema",
                )


def build_substitution_groups(ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any) -> None:
    """Maps substitution-group heads to their member elements.

    XSD 1.0 declares substitution groups on global element
    declarations: a member element carries ``substitutionGroup``
    naming its head. After the ER run, every member is recorded
    under its head's local name so instance parsing can dispatch
    member elements wherever the head is allowed. Heads that name
    no global element are recorded as schema errors.
    """
    # ``schema.elements`` normally holds element declarations, but a
    # malformed schema can put other component kinds there (a
    # top-level group reference, for example); only real element
    # declarations participate in substitution groups.
    elements = [e for e in schemaER.elements if type(e).__name__ == "Element"]
    declaredNames = {element.name for element in elements}
    declaredExpanded = {element.expandedName for element in elements}
    for element in elements:
        heads = element.getSubstitutionGroupHeads(py_xsd)
        if not heads:
            continue
        known = [head for head in heads if head in declaredNames or head in declaredExpanded]
        if not known:
            ctx.report.add_error(
                f"element '{element.name}' declares substitutionGroup "
                f"'{element.tagAttributes.get('substitutionGroup', heads[0])}', "
                "but no global element with that name exists",
                code="unknown-substitution-head",
                element=element.name,
            )
            continue
        for head in dict.fromkeys(known):
            schemaER.substitutionGroups.setdefault(head, []).append(element)
    logger.debug("Substitution groups built: %s", list(schemaER.substitutionGroups))
    report_substitution_group_cycles(ctx, elements, py_xsd)


def report_substitution_group_cycles(
    ctx: CompileContextProtocol, elements: list[Any], py_xsd: Any
) -> None:
    """Reports substitution-group membership cycles.

    A cycle in the ``substitutionGroup`` graph (foo heads bar heads
    foo) has no well-founded head, so the schema is invalid. Each
    element that reaches a cycle while following its head chain is
    reported once; trivial self-reference is included.
    """
    byName: dict[str, Any] = {}
    for element in elements:
        if element.name:
            byName.setdefault(element.name, element)
        expanded = getattr(element, "expandedName", None)
        if expanded:
            byName.setdefault(expanded, element)
    for element in elements:
        if not element.getSubstitutionGroupHeads(py_xsd):
            continue
        # Depth-first walk of the head graph from this element; a
        # path that reaches the element again is a cycle. XSD 1.1
        # allows several heads per member, so every edge is followed.
        stack = [element]
        visited: set[int] = set()
        cycle = False
        while stack:
            current = stack.pop()
            for headName in current.getSubstitutionGroupHeads(py_xsd):
                head = byName.get(headName)
                if head is None:
                    continue
                if head is element:
                    cycle = True
                    break
                if id(head) not in visited:
                    visited.add(id(head))
                    stack.append(head)
            if cycle:
                break
        if cycle:
            ctx.report.add_error(
                f"element '{element.name}' is part of a cyclic substitution group",
                code="circular-substitution-group",
                element=element.name,
                phase="schema",
            )


def check_substitution_group_exclusions(
    ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any
) -> None:
    """Reports a substitution member whose type the head does not admit.

    Element Declaration Properties Correct requires a member's type
    to be validly derived from the head's type, given the head
    element's {substitution group exclusions} — the ``final``
    attribute (or the schema's ``finalDefault``). A type that is
    unrelated to the head's is ``substitution-type``; a derivation
    performed by an excluded method is ``declaration-attribute``.
    Runs after class building so the generated classes carry the
    derivation hierarchy and method.
    """
    finalDefault = schemaER.tagAttributes.get("finalDefault")
    elements = [e for e in schemaER.elements if type(e).__name__ == "Element"]
    byName: dict[str, Any] = {}
    for element in elements:
        if element.name:
            byName.setdefault(element.name, element)
        if getattr(element, "expandedName", None):
            byName.setdefault(element.expandedName, element)
    for member in elements:
        # A member with no explicit ``type`` attribute inherits the
        # head's declaration (the instance binder resolves it the
        # same way), so there is no independent declaration to
        # compare. An inline anonymous type is still checked for an
        # excluded derivation (substGrpExcl00202m2), but an unrelated
        # anonymous type is tolerated: it shares only the ur-type
        # with the head's own anonymous type, which the corpus treats
        # as admissible.
        declaredType = member.tagAttributes.get("type")
        if not declaredType:
            continue
        memberCls = declared_type_class(member)
        if memberCls is None:
            continue
        inline = "|" in declaredType
        headNames = member.getSubstitutionGroupHeads(py_xsd)
        # Enforcing derivation against a single head rejects
        # corpora pyxsd must accept (a member's type may name a base
        # the ur-type-only head relationship tolerates); only a
        # member naming several heads must be validly derived from
        # every one of them (IBM s2_2_2si02), while addB141's
        # single-head list/union mismatch stays a known false
        # accept.
        strict_heads = len(headNames) > 1
        for headName in headNames:
            head = byName.get(headName)
            if head is None or head is member:
                continue
            final = head.tagAttributes.get("final")
            if final is None:
                final = finalDefault
            excluded = blockTokens(final)
            headCls = declared_type_class(head)
            if headCls is None:
                continue
            if headCls is SchemaBase:
                # An untyped head declares the ur-type, from which
                # every type is validly derived: a member can never
                # violate derivation against it (subsgroup001's
                # abstract chapContent/appendixContent heads).
                continue
            reason = is_validly_derived(memberCls, headCls, excluded)
            if reason == "blocked":
                ctx.report.add_error(
                    f"element '{member.name}' has a type whose derivation from "
                    f"substitution head '{head.name}' is excluded by final='{final}'",
                    code="declaration-attribute",
                    element=member.name,
                    phase="schema",
                )
            elif reason == "not-derived" and not inline:
                # pyxsd's lattice does not model the ur-types as a
                # subclass edge, so a plain simple member under an
                # anySimpleType head reports not-derived even though
                # the simple ur-type roots every simple derivation.
                # The ur-type heads are therefore exempt from both the
                # single-head category check and the multi-head
                # ``strict_heads`` report; only the clear category
                # mismatches are reported (stZ048: complex under
                # anySimpleType; stZ049: anySimpleType under a complex
                # head; stZ050/stZ053: anyType admits every type). The
                # general under-approximation for a single ordinary
                # head stays (the shipped substitution fixture).
                head_is_simple_ur = headCls is AnySimpleType
                member_is_simple_ur = memberCls is AnySimpleType
                member_is_complex = getattr(memberCls, "_contentKind_", None) == "complex"
                member_is_union = bool(vars(memberCls).get("_unionMembers"))
                head_admits_all = headCls is AnyType
                head_exempt = head_admits_all or head_is_simple_ur
                mismatch = (strict_heads and not head_exempt) or (
                    not head_admits_all
                    and (member_is_simple_ur or (head_is_simple_ur and member_is_complex))
                )
                # A union is not validly derived from any of its
                # members, so a union-typed member under an ordinary
                # single head violates the derivation clause
                # (particlesZ014/Z021).
                if not head_admits_all and not head_is_simple_ur and member_is_union:
                    mismatch = True
                if mismatch:
                    ctx.report.add_error(
                        f"element '{member.name}' has a type that is not validly "
                        f"derived from substitution head '{head.name}'",
                        code="substitution-type",
                        element=member.name,
                        phase="schema",
                    )


def declared_type_class(element: Any) -> Any:
    """Returns an element's generated type class, or ``None``."""
    try:
        cls = element.getType()
    except (AttributeError, TypeError):
        return None
    return cls if isinstance(cls, type) else None


def check_type_references(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Reports a declaration type QName naming an unloaded namespace.

    The declaration walk tolerates an unresolved type so a large
    schema can still load; this pass turns a reference whose
    resolved QName names a namespace no composed schema declares
    into a schema-phase ``unknown-type``. The loaded-namespace guard
    keeps pyxsd's own resolution gaps (inside a namespace that is
    loaded) out of the report: only a reference the schema itself
    cannot satisfy is a schema error. Runs in strict namespace mode
    only, where a resolved reference has a namespace to test.

    A reference to the unnamed (no-namespace) space is not examined;
    there the plain name is the whole identity and the legacy
    tolerance is the historical behavior. (Extending this pass to
    no-namespace references was tried and reverted: pyxsd does not
    yet resolve every unprefixed reference in the XSD 1.1 feature
    families, so the extension produced 39 vacuous-pass regressions
    across CTA/override/openContent schemas for one real case,
    ``MS-Element/elemM002``, which stays a known false-accept.)
    """
    if getattr(ctx.mode, "namespaces", "legacy") != "strict":
        return
    loaded = loaded_namespaces(ctx)
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ in ("Element", "Attribute") and not (
            getattr(er, "isElementRef", False)
            or getattr(er, "isAttributeRef", False)
            or in_conditional_inclusion(er)
        ):
            raw = er.__dict__.get("type")
            if raw is not None and "|" not in raw:
                resolved = er.resolvedTypeName()
                namespace = namespace_of(resolved) if isinstance(resolved, str) else None
                if namespace is None:
                    # A reference that resolves into no namespace is
                    # normally tolerated (pyxsd does not yet resolve
                    # every unprefixed reference in the XSD 1.1
                    # feature families). Report it only when a global
                    # declaration of the same local name exists in
                    # another loaded namespace: the author meant that
                    # declaration but wrote it unqualified (addB009,
                    # xsd015.e, xsd016.e). Without an actual candidate
                    # the reference stays a known false-accept
                    # (elemM002).
                    # A component spliced in from an included or
                    # redefined (chameleon) document is exempt: its
                    # unprefixed references belong to the adopted
                    # namespace and are resolved by the chameleon pass,
                    # not re-qualified here.
                    if (
                        isinstance(resolved, str)
                        and id(er.xsdElement) not in ctx.composed_element_ids
                    ):
                        report_unqualified_type_candidate(ctx, er, resolved)
                elif namespace not in loaded or namespace == XSD_NS:
                    # The XML Schema namespace is always 'loaded' for
                    # the built-in types, so a reference into it that
                    # names no built-in is reported too (xsd015.e,
                    # xsd016.e, where a default ``xmlns`` of XSD made
                    # an unprefixed name resolve into XSD).
                    try:
                        cls = er.getType()
                    except Exception:  # pragma: no cover - defensive
                        cls = None
                    if cls is None:
                        ctx.report.add_error(
                            f"the type '{resolved}' of "
                            f"{type(er).__name__.lower()} '{er.name}' is not "
                            "declared by any loaded schema",
                            code="unknown-type",
                            element=er.name,
                            phase="schema",
                        )
        stack.extend(getattr(er, "processedChildren", None) or ())


def report_unqualified_type_candidate(ctx: CompileContextProtocol, er: Any, resolved: str) -> None:
    """Reports an unqualified type reference that likely names a candidate.

    A reference that resolves into no namespace is reported only when a
    global type of the same local name is declared in a loaded, non-
    reserved namespace and no no-namespace type of that name exists.
    This keeps the historical tolerance for a genuinely unknown
    unprefixed reference (see ``check_type_references``) while catching
    the real error of writing a type declared in the target namespace
    without its prefix.
    """
    local = _qnameLocal(resolved)
    if not local:
        return
    loaded = loaded_namespaces(ctx)
    reserved = {XSD_NS, XSI_NS, XML_NS, XLINK_NS}
    candidate: str | None = None
    has_no_namespace = False
    for entries in ctx.schema_context.components.values():
        for entry in entries:
            if componentKind(entry) != "type" or getattr(entry, "name", None) != local:
                continue
            namespace = entry.getNamespace()
            if namespace is None:
                has_no_namespace = True
            elif namespace not in reserved and namespace in loaded:
                candidate = namespace
    if has_no_namespace or candidate is None:
        return
    ctx.report.add_error(
        f"the type '{resolved}' of {type(er).__name__.lower()} '{er.name}' is "
        f"not declared in no namespace; a type named '{local}' is declared in "
        f"namespace '{candidate}' and must be referenced with its prefix",
        code="unknown-type",
        element=er.name,
        phase="schema",
    )


def check_simple_content_restriction_base(
    ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any
) -> None:
    """Reports a simpleContent restriction with an illegal base.

    XSD 1.1 (bug 14559, Part 2 §2.4.2.1): the base of a simpleContent
    ``restriction`` must be a complex type whose simple content is not
    ``xs:anySimpleType``. A direct simple-type base (stZ009) and a
    complex base whose content primitive is anySimpleType (stZ007,
    stZ010, stZ047, stZ055) are both invalid; a simpleContent
    ``extension`` of anySimpleType stays valid, and a restriction
    that supplies its own inline ``simpleType`` gives the content a
    non-anySimpleType primitive, so it is legal (IBM s3_12v04).
    """
    from pyxsd.xsd_data_types import AnySimpleType

    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "ComplexType":
            restriction = simple_content_restriction(er)
            if restriction is not None and not getattr(restriction, "hasNoBase", False):
                base_cls = base_type_class(ctx, er, py_xsd)
                if base_cls is not None:
                    has_inline = any(
                        grandchild is not None and type(grandchild).__name__ == "SimpleType"
                        for grandchild in getattr(restriction, "processedChildren", ()) or ()
                    )
                    if getattr(base_cls, "_contentKind_", None) != "complex":
                        ctx.report.add_error(
                            f"the base of the simpleContent restriction of type "
                            f"'{er.name}' is not a complex type",
                            code="invalid-base",
                            element=er.name,
                            phase="schema",
                        )
                    elif issubclass(base_cls, AnySimpleType) and not has_inline:
                        ctx.report.add_error(
                            f"the simpleContent restriction of type '{er.name}' "
                            "derives from a complex type whose simple content is "
                            "xs:anySimpleType",
                            code="invalid-base",
                            element=er.name,
                            phase="schema",
                        )
                    elif getattr(base_cls, "_simpleContentType_", None) is None and not has_inline:
                        # The base is a complex type, but its content is
                        # element-only (or mixed), not simple: a
                        # simpleContent restriction cannot derive from it
                        # (xsd020.e).
                        ctx.report.add_error(
                            f"the base of the simpleContent restriction of type "
                            f"'{er.name}' has element-only content, not simple "
                            "content",
                            code="invalid-base",
                            element=er.name,
                            phase="schema",
                        )
                    elif has_inline:
                        inline_cls = simple_content_inline_class(restriction, py_xsd)
                        base_content = getattr(base_cls, "_simpleContentType_", None)
                        if (
                            inline_cls is not None
                            and isinstance(base_content, type)
                            and is_valid_xsi_type(inline_cls, base_content) == "not-derived"
                        ):
                            # The inline type constrains the base's
                            # simple content, so it must be validly
                            # derived from it (particlesZ018: a list of
                            # int is not derived from xs:decimal).
                            ctx.report.add_error(
                                f"the simpleContent restriction of type "
                                f"'{er.name}' has an inline type that is not "
                                "validly derived from the base's simple content",
                                code="invalid-base",
                                element=er.name,
                                phase="schema",
                            )
        stack.extend(getattr(er, "processedChildren", None) or ())


def simple_content_inline_class(restriction: Any, py_xsd: Any) -> Any:
    """The generated class of a restriction's inline ``simpleType``."""
    for child in getattr(restriction, "processedChildren", ()) or ():
        if child is not None and type(child).__name__ == "SimpleType":
            try:
                return child.clsFor(py_xsd)
            except (AttributeError, TypeError):
                return None
    return None


def check_content_kind_derivation(ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any) -> None:
    """Reports a content kind derived from an inadmissible base kind.

    ``complexContent`` derives from a complex type (or ``xs:anyType``):
    a ``simpleType`` or built-in simple type base is invalid
    (ctJ002/ctJ003). ``simpleContent`` ``extension`` derives from a
    simple type or a complex type whose content is simple; an
    element-only/mixed complex base (ctE003) or ``xs:anyType``
    (ctE004) is invalid. A base that cannot be resolved is left to
    the unknown-type check. ``simpleContent`` ``restriction`` has its
    own rule (``check_simple_content_restriction_base``).
    """
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "ComplexType":
            check_one_content_kind_derivation(ctx, er, py_xsd)
        stack.extend(getattr(er, "processedChildren", None) or ())


def check_one_content_kind_derivation(ctx: CompileContextProtocol, er: Any, py_xsd: Any) -> None:
    """Applies the base-kind rule to one complex type."""
    if er._firstProcessedChild(er, "SimpleContent") is not None:
        if er.getDerivation() != "extension":
            return
        if base_is_any_type(ctx, er, py_xsd):
            ctx.report.add_error(
                f"the base of the simpleContent extension of type "
                f"'{er.name}' is xs:anyType, whose content is not simple",
                code="invalid-base",
                element=er.name,
                phase="schema",
            )
            return
        base_er = base_type_er(ctx, er, py_xsd)
        if base_er is not None and type(base_er).__name__ == "ComplexType":
            variety = base_er._effectiveContentVariety()
            if variety in ("element-only", "mixed"):
                ctx.report.add_error(
                    f"the base of the simpleContent extension of type "
                    f"'{er.name}' has {variety} content, not simple content",
                    code="invalid-base",
                    element=er.name,
                    phase="schema",
                )
        return
    if er._firstProcessedChild(er, "ComplexContent") is None:
        return
    # complexContent: the base must be a complex type (or anyType).
    if base_is_any_type(ctx, er, py_xsd):
        return
    derivation = er.getDerivation()
    if derivation is None:
        return
    base_er = base_type_er(ctx, er, py_xsd)
    if base_er is not None:
        if type(base_er).__name__ != "ComplexType":
            ctx.report.add_error(
                f"the base of the complexContent {derivation} of type "
                f"'{er.name}' is a simple type, not a complex type",
                code="invalid-base",
                element=er.name,
                phase="schema",
            )
        return
    # No ER: a built-in simple type (or an unresolved reference, which
    # the unknown-type check already reports).
    if base_type_class(ctx, er, py_xsd) is not None:
        ctx.report.add_error(
            f"the base of the complexContent {derivation} of type "
            f"'{er.name}' is a simple type, not a complex type",
            code="invalid-base",
            element=er.name,
            phase="schema",
        )


def simple_content_restriction(er: Any) -> Any | None:
    """Returns a complex type's ``simpleContent``/``restriction`` child."""
    for child in er.processedChildren or ():
        if child is not None and type(child).__name__ == "SimpleContent":
            for grandchild in child.processedChildren or ():
                if grandchild is not None and type(grandchild).__name__ == "Restriction":
                    return grandchild
    return None


def is_notation_reference(er: Any, raw: Any, py_xsd: Any) -> bool:
    """Whether a type reference names the built-in ``xs:NOTATION``."""
    if not raw or (isinstance(raw, str) and "|" in raw):
        return False
    resolved = er.resolveSchemaQName(raw, parser=py_xsd)
    if namespace_of(resolved) == XSD_NS and local_name(resolved) == "NOTATION":
        return True
    return raw in ("xs:NOTATION", "xsd:NOTATION")


def check_notation_uses(ctx: CompileContextProtocol, schemaER: Any, py_xsd: Any) -> None:
    """Reports a direct use of ``xs:NOTATION`` without an enumeration.

    XSD Schema Component Constraint: "It is an error for NOTATION to
    be used directly in a schema. Only datatypes that are derived
    from NOTATION by specifying a value for enumeration can be used
    in a schema." A bare ``xs:NOTATION`` element/attribute type or
    list ``itemType`` is therefore invalid (Saxon simple090-simple092);
    a restriction that supplies the enumeration is checked separately
    by ``XsdType._checkNotationRestriction``.

    A ``union`` member is deliberately *not* reported here: the
    corpus is self-contradictory there (MS particlesZ007 expects
    ``union memberTypes="xsd:NOTATION"`` valid while Saxon simple093
    expects it invalid), an open question (w3c/xsdtests#12), so the
    historical acceptance is kept.
    """
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        kind = type(er).__name__
        uses: list[tuple[Any, str]] = []
        if kind in ("Element", "Attribute") and not (
            getattr(er, "isElementRef", False) or getattr(er, "isAttributeRef", False)
        ):
            uses.append((er.__dict__.get("type"), f"{kind.lower()} '{er.name}'"))
        elif kind == "List":
            uses.append((getattr(er, "itemType", None), f"list '{er.getContainingTypeName()}'"))
        for raw, owner in uses:
            if is_notation_reference(er, raw, py_xsd):
                ctx.report.add_error(
                    f"{owner} uses xs:NOTATION directly; a NOTATION type must "
                    "include an enumeration facet",
                    code="notation-enumeration-required",
                    element=er.name,
                    phase="schema",
                )
        stack.extend(getattr(er, "processedChildren", None) or ())


def loaded_namespaces(ctx: CompileContextProtocol) -> set:
    """The namespaces a schema reference may resolve into.

    Every composed target namespace and satisfied import, plus the
    namespaces XSD defines itself (XML Schema, XSI, ``xml`` and
    XLink) and the unnamed space.
    """
    loaded: set = set(ctx.composed_target_namespaces)
    loaded.update(ctx.resolved_imports)
    loaded.update({XSD_NS, XSI_NS, XML_NS, XLINK_NS, None})
    return loaded


def check_alternatives(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Resolves and checks every element's XSD 1.1 type alternatives.

    Runs after the generated classes are built, so each alternative's
    type reference resolves to the same class the instance phase will
    see.  The heavy lifting (resolving each type and checking it is
    validly derived from the declared type) lives in
    :func:`pyxsd.alternatives.check_element_alternatives`.
    """
    from pyxsd.alternatives import check_element_alternatives

    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ == "Element":
            check_element_alternatives(er)
        stack.extend(getattr(er, "processedChildren", None) or ())


def check_form_defaults(ctx: CompileContextProtocol, schemaRoot: Any) -> None:
    """Validates ``elementFormDefault``/``attributeFormDefault`` values.

    ``form`` is an enumeration of ``qualified`` and ``unqualified``
    (XSD 1.1 §3.2.1/§3.3.1). The value is whitespace-collapsed and
    case-sensitive, so the empty string, ``Qualified``,
    ``Unqualified`` and ``qualified unqualified`` are all schema
    errors (MS elemH003-elemH006).
    """
    for attr in ("elementFormDefault", "attributeFormDefault"):
        value = schemaRoot.get(attr)
        if value is None:
            continue
        if value.strip() not in ("qualified", "unqualified"):
            ctx.report.add_error(
                f"the schema declaration's '{attr}' value {value!r} is not "
                "'qualified' or 'unqualified'",
                code="declaration-attribute",
                phase="schema",
            )


def check_value_constraints(ctx: CompileContextProtocol, schemaER: Any) -> None:
    """Validates element/attribute ``default``/``fixed`` values.

    Runs after generated classes are built so a value is checked
    against the declaration's actual type: a user-defined simpleType
    is validated through its constructor (built-in lexical space plus
    every restricting facet), and a complex type with simple content
    delegates to its content type. Built-in types take the same path.
    A declaration whose type has no simple value space (element-only
    complex content, or an unresolved type) is skipped.
    """
    seen: set[int] = set()
    stack = [schemaER]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        if type(er).__name__ in ("Element", "Attribute"):
            check_declaration_value_constraint(ctx, er)
        stack.extend(getattr(er, "processedChildren", None) or ())


def check_declaration_value_constraint(ctx: CompileContextProtocol, er: Any) -> None:

    if type(er).__name__ == "Element" and any(
        attr in er.tagAttributes for attr in ("default", "fixed")
    ):
        try:
            declared = er.getType()
        except (AttributeError, TypeError):
            declared = None
        if (
            isinstance(declared, type)
            and getattr(declared, "_contentKind_", None) == "complex"
            and getattr(declared, "_elementOnly_", False)
        ):
            # e-props-correct: an element whose type has element-only
            # content cannot carry a value constraint (there is no
            # character-content value space to default against).
            ctx.report.add_error(
                f"element '{er.name}' has a default or fixed value but its "
                "type has element-only content",
                code="declaration-attribute",
                element=er.name,
                phase="schema",
            )
            return
    factory = value_constraint_factory(er)
    if factory is None:
        return
    label = type(er).__name__.lower()
    for attr in ("default", "fixed"):
        value = er.tagAttributes.get(attr)
        if value is None:
            continue
        try:
            factory(value)
        except (TypeError, ValueError):
            ctx.report.add_error(
                f"{label} '{er.name}' {attr} value '{value}' is not valid for its type",
                code="declaration-attribute",
                element=er.name,
                phase="schema",
            )
        except Exception:  # pragma: no cover - defensive
            # A constructor raising anything else is a pyxsd bug, not
            # a schema error; do not turn it into a false rejection.
            logger.debug("could not validate %s value %r of %r", attr, value, er.name)


def value_constraint_factory(er: Any) -> Any:
    """Returns the class that validates a declaration's lexical value.

    ``None`` when the declaration's type has no simple value space,
    which leaves element-only complex content and unresolved types
    unchecked.
    """
    try:
        cls = er.getType()
    except (AttributeError, TypeError):
        return None
    if not isinstance(cls, type):
        return None
    content = getattr(cls, "_simpleContentType_", None)
    if isinstance(content, type):
        cls = content
    if not issubclass(cls, XsdDataType):
        return None
    return cls
