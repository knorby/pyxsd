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
          namespace; the absent namespace is never admitted.
        * A literal URI admits exactly that namespace.
        """
        target = self.effective_target(target_namespace)
        for token in self.namespace.split():
            if token == NAMESPACE_ANY:
                return True
            if token == NAMESPACE_LOCAL:
                if uri is None:
                    return True
            elif token == NAMESPACE_TARGET:
                if uri == target:
                    return True
            elif token == NAMESPACE_OTHER:
                if uri is not None and uri != target:
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


__all__ = [
    "NAMESPACE_ANY",
    "NAMESPACE_LOCAL",
    "NAMESPACE_OTHER",
    "NAMESPACE_TARGET",
    "PROCESS_CONTENTS",
    "WildcardSpec",
    "register_wildcard",
    "wildcard_spec",
    "wildcard_specs_overlap",
]
