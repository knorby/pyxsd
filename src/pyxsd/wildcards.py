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


@dataclass(frozen=True)
class WildcardSpec:
    """The namespace/processContents configuration of one wildcard.

    ``namespace`` is the raw constraint attribute (whitespace-separated
    keywords and/or URIs; missing means ``##any``). ``process_contents``
    is ``"skip"``, ``"lax"`` or ``"strict"``. ``is_attribute`` records
    which wildcard kind produced the spec.
    """

    namespace: str = NAMESPACE_ANY
    process_contents: str = "strict"
    is_attribute: bool = False

    def allows(self, uri: str | None, target_namespace: str | None) -> bool:
        """Whether a node in namespace ``uri`` satisfies this constraint.

        ``uri`` is ``None`` for the absent (unqualified) namespace;
        ``target_namespace`` is the containing schema's target namespace
        (also possibly ``None``). The declared semantics:

        * ``##any`` admits everything.
        * ``##local`` admits only the absent namespace.
        * ``##targetNamespace`` admits the target namespace (the absent
          namespace when the schema has no target namespace).
        * ``##other`` admits any namespace except the target namespace,
          but never the absent namespace when that *is* the target
          namespace.
        * A literal URI admits exactly that namespace.
        """
        for token in self.namespace.split():
            if token == NAMESPACE_ANY:
                return True
            if token == NAMESPACE_LOCAL:
                if uri is None:
                    return True
            elif token == NAMESPACE_TARGET:
                if uri == target_namespace:
                    return True
            elif token == NAMESPACE_OTHER:
                if uri == target_namespace:
                    continue
                if uri is not None or target_namespace is not None:
                    return True
            elif not token.startswith("##") and token == uri:
                return True
        return False


def wildcard_spec(attributes: Mapping[str, Any], *, is_attribute: bool = False) -> WildcardSpec:
    """Builds a :class:`WildcardSpec` from an ER's ``tagAttributes``.

    Missing ``namespace`` defaults to ``##any``; an unrecognized
    ``processContents`` falls back to ``strict`` (the schema default) so
    a malformed value cannot silently disable validation.
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


__all__ = [
    "NAMESPACE_ANY",
    "NAMESPACE_LOCAL",
    "NAMESPACE_OTHER",
    "NAMESPACE_TARGET",
    "PROCESS_CONTENTS",
    "WildcardSpec",
    "register_wildcard",
    "wildcard_spec",
]
