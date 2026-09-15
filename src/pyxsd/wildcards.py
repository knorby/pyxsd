"""Namespace and ``processContents`` constraints for ``xs:any`` wildcards.

The XSD wildcard (``xs:any`` / ``xs:anyAttribute``) carries two pieces of
configuration: a **namespace constraint** saying which namespaces the
wildcard admits, and a **processContents** mode saying how strongly a
matched item is validated. This module keeps that configuration in one
small value object so the element and attribute wildcard ERs, the content
model, and the binding loop all read the same interpretation.

Only the standard library is used; the module is deliberately free of
imports from the rest of pyxsd so it can be imported from anywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The namespace-constraint keywords defined by XML Schema.
NAMESPACE_ANY = "##any"
NAMESPACE_OTHER = "##other"
NAMESPACE_LOCAL = "##local"
NAMESPACE_TARGET = "##targetNamespace"

#: The ``processContents`` values; anything else is treated as ``strict``.
PROCESS_CONTENTS = frozenset({"skip", "lax", "strict"})

#: ``processContents`` severity; a higher value is a stricter check.
PROCESS_SEVERITY = {"skip": 0, "lax": 1, "strict": 2}

#: The unqualified XML attributes the ``xs:any`` representation takes.
#: ``notNamespace``/``notQName`` are XSD 1.1 attributes; the corpus runs
#: under the 1.1 profile, where expected-valid schemas carry them, so
#: they are accepted here (their semantics are a separate task).
ANY_XML_ATTRIBUTES = frozenset(
    {"id", "minOccurs", "maxOccurs", "namespace", "processContents", "notNamespace", "notQName"}
)

#: The unqualified XML attributes the ``xs:anyAttribute`` representation
#: takes (occurrence attributes belong to particles only).
ANY_ATTRIBUTE_XML_ATTRIBUTES = frozenset(
    {"id", "namespace", "processContents", "notNamespace", "notQName"}
)

#: Sentinel for "the wildcard's source namespace was not recorded";
#: namespace keywords then resolve against the matching schema.
_UNSET: Any = object()


@dataclass(frozen=True)
class WildcardSpec:
    """The namespace/processContents configuration of one wildcard.

    ``namespace`` is the raw constraint attribute (whitespace-separated
    keywords and/or URIs; missing means ``##any``). ``process_contents``
    is ``"skip"``, ``"lax"`` or ``"strict"``. ``is_attribute`` records
    which wildcard kind produced the spec.

    ``target_namespace`` is the target namespace of the document the
    wildcard was *declared in*. ``##targetNamespace`` and ``##other``
    resolve against that document, not against a schema that later
    inherits the wildcard through an extension or restriction.
    """

    namespace: str = NAMESPACE_ANY
    process_contents: str = "strict"
    is_attribute: bool = False
    target_namespace: Any = _UNSET

    def effective_target(self, target_namespace: str | None) -> str | None:
        """The namespace ``##targetNamespace``/``##other`` resolve to."""
        if self.target_namespace is _UNSET:
            return target_namespace
        return self.target_namespace

    def allows(self, uri: str | None, target_namespace: str | None) -> bool:
        """Whether a node in namespace ``uri`` satisfies this constraint.

        ``uri`` is ``None`` for the absent (unqualified) namespace;
        ``target_namespace`` is the fallback target namespace when the
        spec does not carry its source document's. The declared
        semantics:

        * ``##any`` admits everything.
        * ``##local`` admits only the absent namespace.
        * ``##targetNamespace`` admits the target namespace (the absent
          namespace when the schema has no target namespace).
        * ``##other`` admits any present namespace except the target
          namespace and any literal URI written after it (the internal
          representation of a computed ``not(S)`` constraint); the
          absent namespace is admitted only when ``##local`` is also
          present.
        * A literal URI admits exactly that namespace.
        """
        target = self.effective_target(target_namespace)
        tokens = self.namespace.split()
        if NAMESPACE_ANY in tokens:
            return True
        if tokens and tokens[0] == NAMESPACE_OTHER:
            if uri is None:
                return NAMESPACE_LOCAL in tokens
            excluded = {token for token in tokens[1:] if not token.startswith("##")}
            return uri != target and uri not in excluded
        for token in tokens:
            if token == NAMESPACE_LOCAL:
                if uri is None:
                    return True
            elif token == NAMESPACE_TARGET:
                if uri == target:
                    return True
            elif not token.startswith("##") and token == uri:
                return True
        return False


def wildcard_spec(
    attributes: Mapping[str, Any],
    *,
    is_attribute: bool = False,
    target_namespace: Any = _UNSET,
) -> WildcardSpec:
    """Builds a :class:`WildcardSpec` from an ER's ``tagAttributes``.

    Missing ``namespace`` defaults to ``##any``; an unrecognized
    ``processContents`` falls back to ``strict`` (the schema default) so
    a malformed value cannot silently disable validation. Pass the
    declaring document's ``targetNamespace`` as ``target_namespace`` so
    namespace keywords keep their source meaning.
    """
    raw_namespace = attributes.get("namespace")
    raw_process = attributes.get("processContents")
    process = str(raw_process).strip() if raw_process is not None else "strict"
    if process not in PROCESS_CONTENTS:
        process = "strict"
    return WildcardSpec(
        namespace=NAMESPACE_ANY if raw_namespace is None else str(raw_namespace),
        process_contents=process,
        is_attribute=is_attribute,
        target_namespace=target_namespace,
    )


def invalid_namespace_constraint(value: str | None) -> str | None:
    """The offending token when ``namespace`` is not a legal constraint.

    Legal values are ``##any`` alone, ``##other`` alone, or a
    whitespace-separated list whose tokens are each a URI reference,
    ``##local`` or ``##targetNamespace``. An unknown ``##`` keyword or a
    ``##any``/``##other`` mixed with anything else is illegal; a token
    that does not start with ``##`` is a URI reference (a relative
    reference and a QName-like ``a:b`` are both legal). An empty (or
    absent) constraint is not reported here.
    """
    if value is None:
        return None
    tokens = str(value).split()
    if not tokens or tokens in ([NAMESPACE_ANY], [NAMESPACE_OTHER]):
        return None
    for token in tokens:
        if token in (NAMESPACE_LOCAL, NAMESPACE_TARGET):
            continue
        if token.startswith("##"):
            return token
    return None


def invalid_process_contents(value: str | None) -> str | None:
    """The raw value when ``processContents`` is present and illegal.

    The value must be exactly one of ``skip``/``lax``/``strict``
    (surrounding whitespace is tolerated). The absent attribute is not
    illegal here; it means the ``strict`` default.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text in PROCESS_CONTENTS:
        return None
    return str(value)


def wildcard_declaration_problems(
    attributes: Mapping[str, Any], *, is_attribute: bool
) -> list[tuple[str, str]]:
    """The schema problems in one wildcard's XML attributes.

    Returns ``(code, message)`` pairs for the caller to report:

    * ``wildcard-invalid`` for an illegal namespace-constraint token, a
      ``processContents`` value other than ``skip``/``lax``/``strict``,
      or an occurrence attribute on ``xs:anyAttribute`` (which is not a
      particle);
    * ``invalid-attribute`` for an unqualified XML attribute outside the
      wildcard's allowed set. Qualified attributes (Clark names) are
      foreign and never reported, and namespace declarations are not
      attributes.

    An empty ``namespace`` constraint is not reported: the corpus pins
    such a schema valid (wildZ010), even though it admits nothing.
    """
    tag = "xs:anyAttribute" if is_attribute else "xs:any"
    allowed = ANY_ATTRIBUTE_XML_ATTRIBUTES if is_attribute else ANY_XML_ATTRIBUTES
    problems: list[tuple[str, str]] = []
    raw_namespace = attributes.get("namespace")
    token = invalid_namespace_constraint(raw_namespace)
    if token is not None:
        problems.append(
            (
                "wildcard-invalid",
                f"<{tag}> namespace constraint '{raw_namespace}' is not legal: "
                f"'{token}' is not a legal token",
            )
        )
    raw_process = attributes.get("processContents")
    if invalid_process_contents(raw_process) is not None:
        problems.append(
            (
                "wildcard-invalid",
                f"<{tag}> processContents '{raw_process}' is not one of 'skip', 'lax', 'strict'",
            )
        )
    handled = set()
    if is_attribute:
        for name in ("minOccurs", "maxOccurs"):
            if name in attributes:
                problems.append(
                    (
                        "wildcard-invalid",
                        f"<{tag}> must not carry an occurrence attribute '{name}'",
                    )
                )
                handled.add(name)
    for name in attributes:
        if name.startswith("{") or name in allowed or name in handled:
            continue
        problems.append(("invalid-attribute", f"<{tag}> has an invalid attribute '{name}'"))
    return problems


def register_wildcard(containing_type: Any, spec: WildcardSpec) -> None:
    """Records ``spec`` on a type/group and flips its wildcard flag.

    Specs are accumulated on ``wildcardElementSpecs`` /
    ``wildcardAttributeSpecs`` (created lazily as plain lists) so several
    wildcards in one content model can each contribute their constraint.
    Duplicates are dropped so a group referenced repeatedly does not
    multiply its wildcards.
    """
    if spec.is_attribute:
        specs = list(getattr(containing_type, "wildcardAttributeSpecs", ()))
        if spec not in specs:
            specs.append(spec)
        containing_type.wildcardAttributeSpecs = specs
        containing_type.hasWildcardAttributes = True
        return
    specs = list(getattr(containing_type, "wildcardElementSpecs", ()))
    if spec not in specs:
        specs.append(spec)
    containing_type.wildcardElementSpecs = specs
    containing_type.hasWildcardElements = True


def _wildcard_admission(
    spec: WildcardSpec, target_namespace: str | None
) -> tuple[str, frozenset[str], bool, bool]:
    """Expands a namespace constraint into comparable admission parts.

    Returns ``(kind, uris, local, target)`` where ``kind`` is ``"any"``
    (everything), ``"other"`` (any present namespace except the
    excluded ones) or ``"set"``; for ``"other"`` the ``uris`` set holds
    the *excluded* namespaces (the target namespace and the optional
    ``##other uri`` exclusion), for ``"set"`` it holds the *admitted*
    literal URIs. ``local`` admits the absent namespace and ``target``
    admits the target namespace.
    """
    tokens = spec.namespace.split() or [NAMESPACE_ANY]
    if NAMESPACE_ANY in tokens:
        return ("any", frozenset(), False, False)
    if tokens[0] == NAMESPACE_OTHER:
        # "##other" may be followed by a single URI: everything except
        # the target namespace *and* that URI.
        excluded = {token for token in tokens[1:] if not token.startswith("##")}
        if target_namespace is not None:
            excluded.add(target_namespace)
        return ("other", frozenset(excluded), False, False)
    uris = frozenset(token for token in tokens if not token.startswith("##"))
    return (
        "set",
        uris,
        NAMESPACE_LOCAL in tokens,
        NAMESPACE_TARGET in tokens,
    )


def wildcard_specs_overlap(
    first: WildcardSpec,
    second: WildcardSpec,
    target_namespace: str | None = None,
) -> bool:
    """Whether two wildcard namespace constraints admit a common node.

    Used by the content-model sweep for the UPA rule on ``all`` groups:
    two wildcards in the same ``all`` must not overlap (all243), while
    disjoint URI lists are fine (all005). ``##any`` overlaps
    everything; ``##other`` overlaps ``##other`` (both admit every
    third-party namespace) and any literal URI it does not exclude;
    ``##local`` only overlaps ``##local`` (and ``##any``), never
    ``##other``; ``##targetNamespace`` overlaps ``##targetNamespace`` and
    a URI list containing the target namespace, but never ``##other``.
    """
    kind_a, uris_a, local_a, target_a = _wildcard_admission(first, target_namespace)
    kind_b, uris_b, local_b, target_b = _wildcard_admission(second, target_namespace)
    if kind_a == "any" or kind_b == "any":
        return True
    if kind_a == "other" and kind_b == "other":
        return True
    if kind_a == "other" or kind_b == "other":
        excluded = uris_a if kind_a == "other" else uris_b
        uris = uris_b if kind_a == "other" else uris_a
        local = local_b if kind_a == "other" else local_a
        target = target_b if kind_a == "other" else target_a
        if local or target:
            # "##other" never admits the absent namespace or the target.
            return False
        return any(uri not in excluded for uri in uris)
    if local_a and local_b:
        return True
    if target_a and target_b:
        return True
    if uris_a & uris_b:
        return True
    return bool(target_b and target_namespace in uris_a) or bool(
        target_a and target_namespace in uris_b
    )


def _constraint_parts(
    spec: WildcardSpec, target_namespace: str | None
) -> tuple[str, frozenset[str], bool]:
    """Normalizes a constraint into ``(kind, uris, local)``.

    ``kind`` is ``"any"``, ``"other"`` (``uris`` holds the *excluded*
    namespaces, resolved against the spec's own target) or ``"set"``
    (``uris`` holds the admitted literal namespaces and ``local`` says
    the absent namespace is admitted). An empty constraint admits
    nothing. This mirrors :meth:`WildcardSpec.allows`, so a computed
    constraint can be re-normalized by a later combination.
    """
    tokens = spec.namespace.split()
    if not tokens:
        return ("set", frozenset(), False)
    if NAMESPACE_ANY in tokens:
        return ("any", frozenset(), False)
    if tokens[0] == NAMESPACE_OTHER:
        excluded = {token for token in tokens[1:] if not token.startswith("##")}
        target = spec.effective_target(target_namespace)
        if target is not None:
            excluded.add(target)
        return ("other", frozenset(excluded), NAMESPACE_LOCAL in tokens)
    uris = {token for token in tokens if not token.startswith("##")}
    local = NAMESPACE_LOCAL in tokens
    if NAMESPACE_TARGET in tokens:
        target = spec.effective_target(target_namespace)
        if target is None:
            local = True
        else:
            uris.add(target)
    return ("set", frozenset(uris), local)


def _severity(process_contents: str) -> int:
    """The ``processContents`` severity, malformed values reading strict."""
    return PROCESS_SEVERITY.get(process_contents, PROCESS_SEVERITY["strict"])


def _process_contents_name(severity: int) -> str:
    for name, value in PROCESS_SEVERITY.items():
        if value == severity:
            return name
    return "strict"


def _combine(
    base: WildcardSpec,
    own: WildcardSpec,
    namespace: str,
    severity: int,
    target_namespace: Any,
) -> WildcardSpec:
    """Builds a computed spec from two source specs."""
    return WildcardSpec(
        namespace=namespace,
        process_contents=_process_contents_name(severity),
        is_attribute=base.is_attribute or own.is_attribute,
        target_namespace=target_namespace,
    )


def _other_namespace(excluded: frozenset[str], target: str | None) -> str:
    """The ``##other`` constraint excluding ``target`` and ``excluded``.

    Extra exclusions are written as trailing literal URIs; the caller
    must ensure at least the target (or one exclusion) is present, since
    an empty exclusion set is not expressible as ``##other``.
    """
    extras = sorted(uri for uri in excluded if uri != target)
    if not extras:
        return NAMESPACE_OTHER
    return f"{NAMESPACE_OTHER} {' '.join(extras)}"


def intersect_wildcard_specs(
    base: WildcardSpec,
    own: WildcardSpec,
    target_namespace: str | None = None,
) -> WildcardSpec:
    """The intersection of two attribute-wildcard constraints.

    Namespace constraints intersect (errata E1-10): ``##any`` yields the
    other side, two ``##other`` constraints exclude the union of their
    exclusions, ``##other`` against a list keeps the listed namespaces
    the ``##other`` does not exclude, and two lists intersect. The absent
    namespace is admitted only when *both* constraints admit it — a
    computed ``##other … ##local`` (an extension union) does — and is
    written as ``##local`` in the result. The intersection takes the
    *weaker* ``processContents`` of the two (``skip`` < ``lax`` <
    ``strict``); the caller decides whether a derived type may weaken its
    base by comparing severities.
    """
    kind_base, uris_base, local_base = _constraint_parts(base, target_namespace)
    kind_own, uris_own, local_own = _constraint_parts(own, target_namespace)
    severity = min(_severity(base.process_contents), _severity(own.process_contents))
    local = local_base and local_own
    if kind_base == "any" and kind_own == "any":
        return _combine(base, own, NAMESPACE_ANY, severity, target_namespace)
    if kind_base == "any":
        return _combine(base, own, own.namespace, severity, own.target_namespace)
    if kind_own == "any":
        return _combine(base, own, base.namespace, severity, base.target_namespace)
    if kind_base == "other" and kind_own == "other":
        excluded = uris_base | uris_own
        target = base.effective_target(target_namespace)
        if target is None:
            target = own.effective_target(target_namespace)
        namespace = _other_namespace(excluded, target)
        if local:
            namespace = f"{namespace} {NAMESPACE_LOCAL}"
        return _combine(base, own, namespace, severity, target)
    if kind_base == "other":
        tokens = sorted(uris_own - uris_base) + ([NAMESPACE_LOCAL] if local else [])
        return _combine(base, own, " ".join(tokens), severity, target_namespace)
    if kind_own == "other":
        tokens = sorted(uris_base - uris_own) + ([NAMESPACE_LOCAL] if local else [])
        return _combine(base, own, " ".join(tokens), severity, target_namespace)
    uris = uris_base & uris_own
    tokens = sorted(uris) + ([NAMESPACE_LOCAL] if local else [])
    return _combine(base, own, " ".join(tokens), severity, target_namespace)


def union_wildcard_specs(
    base: WildcardSpec,
    own: WildcardSpec,
    target_namespace: str | None = None,
) -> WildcardSpec:
    """The union of two attribute-wildcard constraints (extension).

    Both wildcards apply to the derived type, so the union admits every
    namespace either side admits (errata E1-10): ``##any`` wins, two
    ``##other`` constraints exclude only the intersection of their
    exclusions, and a list unions into / against the other side. The union
    takes the *stronger* ``processContents``: a single combined spec
    must not weaken an obligation that either source wildcard imposes.

    A union admits the absent namespace only when a source wildcard
    admits it (``##local``/``##any``): when the exclusion sets cancel
    but neither side admits the absent namespace, the result is the
    "every present namespace, not absent" constraint, not ``##any``.
    """
    kind_base, uris_base, local_base = _constraint_parts(base, target_namespace)
    kind_own, uris_own, local_own = _constraint_parts(own, target_namespace)
    severity = max(_severity(base.process_contents), _severity(own.process_contents))
    local = local_base or local_own
    if kind_base == "any" or kind_own == "any":
        return _combine(base, own, NAMESPACE_ANY, severity, target_namespace)
    if kind_base == "other" and kind_own == "other":
        excluded = uris_base & uris_own
        target = base.effective_target(target_namespace)
        if target is None:
            target = own.effective_target(target_namespace)
        if not excluded:
            return _absent_union_result(base, own, local, severity, target_namespace)
        return _other_union_result(base, own, excluded, target, local, severity)
    if kind_base == "other" or kind_own == "other":
        other_excluded = uris_base if kind_base == "other" else uris_own
        admitted = uris_own if kind_base == "other" else uris_base
        excluded = other_excluded - admitted
        if not excluded:
            return _absent_union_result(base, own, local, severity, target_namespace)
        target = base.effective_target(target_namespace)
        if target is None:
            target = own.effective_target(target_namespace)
        return _other_union_result(base, own, excluded, target, local, severity)
    uris = uris_base | uris_own
    tokens = sorted(uris) + ([NAMESPACE_LOCAL] if local else [])
    return _combine(base, own, " ".join(tokens), severity, target_namespace)


def _absent_union_result(
    base: WildcardSpec,
    own: WildcardSpec,
    local: bool,
    severity: int,
    target_namespace: str | None,
) -> WildcardSpec:
    """The union result when no present namespace stays excluded."""
    if local:
        return _combine(base, own, NAMESPACE_ANY, severity, target_namespace)
    # Every present namespace, the absent namespace still excluded:
    # ``##other`` with no target admits exactly that.
    return _combine(base, own, NAMESPACE_OTHER, severity, None)


def _other_union_result(
    base: WildcardSpec,
    own: WildcardSpec,
    excluded: frozenset[str],
    target: str | None,
    local: bool,
    severity: int,
) -> WildcardSpec:
    """A union result with a non-empty present-namespace exclusion set."""
    if target is not None and target in excluded:
        namespace = _other_namespace(excluded, target)
        spec_target = target
    else:
        # The stored target must not over-exclude: write every excluded
        # namespace as a literal and carry no target.
        namespace = _other_namespace(excluded, None)
        spec_target = None
    if local:
        namespace = f"{namespace} {NAMESPACE_LOCAL}"
    return _combine(base, own, namespace, severity, spec_target)


def effective_attribute_wildcard(
    specs: Any,
    target_namespace: str | None = None,
) -> WildcardSpec | None:
    """The effective attribute wildcard of one type definition.

    Several wildcards can apply to one type — a local ``anyAttribute``
    and the ones contributed by its attribute groups, or the steps of a
    restriction chain — and they combine by intersection. Returns
    ``None`` when no spec applies.
    """
    result: WildcardSpec | None = None
    for spec in specs:
        result = (
            spec if result is None else intersect_wildcard_specs(result, spec, target_namespace)
        )
    return result


__all__ = [
    "ANY_ATTRIBUTE_XML_ATTRIBUTES",
    "ANY_XML_ATTRIBUTES",
    "NAMESPACE_ANY",
    "NAMESPACE_LOCAL",
    "NAMESPACE_OTHER",
    "NAMESPACE_TARGET",
    "PROCESS_CONTENTS",
    "PROCESS_SEVERITY",
    "WildcardSpec",
    "effective_attribute_wildcard",
    "intersect_wildcard_specs",
    "invalid_namespace_constraint",
    "invalid_process_contents",
    "register_wildcard",
    "union_wildcard_specs",
    "wildcard_declaration_problems",
    "wildcard_spec",
    "wildcard_specs_overlap",
]
