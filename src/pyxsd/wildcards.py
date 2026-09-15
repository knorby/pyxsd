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

#: The XSD 1.1 ``notQName`` keywords. They denote names *defined* in the
#: instance's schema context (``##defined``) and names declared in the
#: wildcard's own content model (``##definedSibling``); both are stored
#: verbatim and only applied when a caller supplies the corresponding
#: name set (the binding phase).
DISALLOWED_DEFINED = "##defined"
DISALLOWED_SIBLING = "##definedSibling"

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

    The XSD 1.1 exclusion sets are ``not_namespace`` (raw namespace
    tokens: literal URIs plus ``##local``/``##targetNamespace``, which
    replace the ``namespace`` constraint) and ``not_qname`` (expanded
    names as Clark strings, a bare ``{uri}`` entry for "every local in
    ``uri``", and the ``##defined``/``##definedSibling`` keywords stored
    verbatim). ``not_namespace`` is an alternative to ``namespace``:
    when it is present the wildcard admits exactly the namespaces it
    does not list.
    """

    namespace: str = NAMESPACE_ANY
    process_contents: str = "strict"
    is_attribute: bool = False
    target_namespace: Any = _UNSET
    not_namespace: frozenset[str] = frozenset()
    not_qname: frozenset[str] = frozenset()

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

    def excludes_namespace(self, uri: str | None, target_namespace: str | None) -> bool:
        """Whether the ``notNamespace`` list excludes ``uri``.

        ``##local`` stands for the absent namespace; ``##targetNamespace``
        resolves the same way the main constraint does (against the
        spec's recorded source namespace, or ``target_namespace`` when
        it carries none).
        """
        target = self.effective_target(target_namespace)
        for token in self.not_namespace:
            if token == NAMESPACE_LOCAL:
                if uri is None:
                    return True
            elif token == NAMESPACE_TARGET:
                if uri == target:
                    return True
            elif not token.startswith("##") and token == uri:
                return True
        return False

    def admits_namespace(self, uri: str | None, target_namespace: str | None) -> bool:
        """Whether the wildcard's namespace constraint admits ``uri``.

        The XSD 1.1 ``notNamespace`` attribute is an alternative to
        ``namespace`` (the two are mutually exclusive in a schema
        document): when it is present the constraint has the "not"
        variety and only the exclusion list decides admission.
        """
        if self.not_namespace:
            return not self.excludes_namespace(uri, target_namespace)
        return self.allows(uri, target_namespace)

    def excludes_name(
        self,
        name: str,
        *,
        defined: Any = None,
        siblings: Any = None,
    ) -> bool:
        """Whether ``notQName`` excludes the expanded ``name``.

        ``name`` is a Clark name or a bare local for the absent
        namespace. A bare ``{uri}`` entry excludes every local in
        ``uri``. The ``##defined`` / ``##definedSibling`` keywords are
        applied only when the corresponding set is supplied (the
        binding phase knows the instance's schema context); with the set
        ``None`` the keyword is not applied. Each set holds names in the
        same Clark/bare form as ``name``.
        """
        if DISALLOWED_DEFINED in self.not_qname and defined is not None and name in defined:
            return True
        if DISALLOWED_SIBLING in self.not_qname and siblings is not None and name in siblings:
            return True
        uri, local = split_name(name)
        for token in self.not_qname:
            match = disallowed_name_parts(token)
            if match is None:
                continue
            token_uri, token_local = match
            if token_uri == uri and (token_local is None or token_local == local):
                return True
        return False

    def allows_name(
        self,
        name: str,
        target_namespace: str | None,
        *,
        defined: Any = None,
        siblings: Any = None,
    ) -> bool:
        """Whether an expanded name satisfies this wildcard.

        ``name`` is a Clark name (``{uri}local``) or a bare local for
        the absent namespace. The name is allowed when its namespace is
        admitted by the namespace constraint (including the 1.1
        ``notNamespace`` exclusions) and it is not excluded by
        ``notQName``.
        """
        uri, _local = split_name(name)
        if not self.admits_namespace(uri, target_namespace):
            return False
        return not self.excludes_name(name, defined=defined, siblings=siblings)


def split_name(name: str) -> tuple[str | None, str]:
    """The ``(namespace, local)`` parts of a Clark name or bare local."""
    if name.startswith("{"):
        uri, _, local = name[1:].partition("}")
        return uri or None, local
    return None, name


def disallowed_name_parts(token: str) -> tuple[str | None, str | None] | None:
    """``(namespace, local)`` for a ``notQName`` member.

    ``local`` is ``None`` when the token is a bare ``{uri}`` entry,
    which excludes every local in ``uri`` (the ``{uri}*`` form is
    normalized to this at parse time). ``None`` as the whole result
    means the token cannot name a real expanded name — a reserved
    keyword or a raw token whose prefix could not be resolved — and it
    therefore never matches an instance name.
    """
    if token.startswith("{"):
        uri, _, local = token[1:].partition("}")
        if local in ("", "*"):
            return uri or None, None
        return uri or None, local
    if ":" in token or token.startswith("##"):
        return None
    return None, token


def excluded_locals(spec: WildcardSpec, uri: str | None) -> frozenset[str]:
    """The local names in ``uri`` the spec's exact ``notQName`` entries cover."""
    found: set[str] = set()
    for token in spec.not_qname:
        match = disallowed_name_parts(token)
        if match is None:
            continue
        token_uri, token_local = match
        if token_uri == uri and token_local is not None:
            found.add(token_local)
    return frozenset(found)


def excludes_all_locals(spec: WildcardSpec, uri: str | None) -> bool:
    """Whether a bare ``{uri}`` entry excludes every local in ``uri``."""
    for token in spec.not_qname:
        match = disallowed_name_parts(token)
        if match is None:
            continue
        token_uri, token_local = match
        if token_uri == uri and token_local is None:
            return True
    return False


def _normalize_not_qname(expanded: str) -> str:
    """Normalizes an expanded ``notQName`` token.

    The ``{uri}*`` form (every local in ``uri``) is stored as the bare
    Clark name ``{uri}`` so one representation covers both spellings.
    """
    if expanded.endswith("}*"):
        return expanded[:-1]
    return expanded


def _parse_not_qname(raw: Any, resolve_qname: Any) -> tuple[str, ...]:
    """The stored tokens of a raw ``notQName`` attribute.

    Reserved keywords and already-expanded Clark names are kept as
    written; a lexical QName is passed through ``resolve_qname`` when
    one is supplied (the caller owns prefix resolution — this module
    stays free of pyxsd imports). A token whose resolution fails is kept
    raw: the declaration check reports it, and a raw token simply never
    matches an expanded name.
    """
    if raw is None:
        return ()
    tokens: list[str] = []
    for token in str(raw).split():
        if token.startswith("{") or token.startswith("##"):
            tokens.append(_normalize_not_qname(token))
            continue
        if resolve_qname is not None:
            try:
                expanded = resolve_qname(token)
            except Exception:
                tokens.append(token)
                continue
            tokens.append(_normalize_not_qname(str(expanded)))
            continue
        tokens.append(token)
    return tuple(tokens)


def wildcard_spec(
    attributes: Mapping[str, Any],
    *,
    is_attribute: bool = False,
    target_namespace: Any = _UNSET,
    resolve_qname: Any = None,
) -> WildcardSpec:
    """Builds a :class:`WildcardSpec` from an ER's ``tagAttributes``.

    Missing ``namespace`` defaults to ``##any``; an unrecognized
    ``processContents`` falls back to ``strict`` (the schema default) so
    a malformed value cannot silently disable validation. Pass the
    declaring document's ``targetNamespace`` as ``target_namespace`` so
    namespace keywords keep their source meaning.

    ``resolve_qname`` optionally maps a lexical QName in ``notQName`` to
    its expanded (Clark) form; the caller injects it because prefix
    bindings live in the parsing layer. Without it, ``notQName`` tokens
    are stored as written.
    """
    raw_namespace = attributes.get("namespace")
    raw_process = attributes.get("processContents")
    process = str(raw_process).strip() if raw_process is not None else "strict"
    if process not in PROCESS_CONTENTS:
        process = "strict"
    raw_not_namespace = attributes.get("notNamespace")
    not_namespace = (
        frozenset(str(raw_not_namespace).split()) if raw_not_namespace is not None else frozenset()
    )
    return WildcardSpec(
        namespace=NAMESPACE_ANY if raw_namespace is None else str(raw_namespace),
        process_contents=process,
        is_attribute=is_attribute,
        target_namespace=target_namespace,
        not_namespace=not_namespace,
        not_qname=frozenset(_parse_not_qname(attributes.get("notQName"), resolve_qname)),
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


def invalid_not_namespace(value: str | None) -> str | None:
    """The offending token when ``notNamespace`` is not a legal constraint.

    ``notNamespace`` is an alternative to ``namespace`` and takes the
    same list vocabulary: whitespace-separated URI references plus
    ``##local`` and ``##targetNamespace``. Unlike ``namespace`` it never
    takes ``##any``/``##other`` (the "any" and "other" constraints have
    no exclusion-list spelling); duplicates are legal. An empty (or
    absent) value is not reported here.
    """
    if value is None:
        return None
    for token in str(value).split():
        if token in (NAMESPACE_LOCAL, NAMESPACE_TARGET):
            continue
        if token.startswith("##"):
            return token
    return None


def invalid_not_qname(value: str | None, *, resolve_qname: Any = None) -> str | None:
    """The offending token when ``notQName`` is not a legal name list.

    Legal entries are ``##defined``/``##definedSibling`` or a lexical
    QName. A QName has at most one colon between a non-empty prefix and
    a non-empty local part; when ``resolve_qname`` is supplied the
    prefix must also resolve (the ``xml`` prefix counts as declared and
    is supplied by the ambient namespace context). Unprefixed names are
    QNames too (in the absent namespace, or the default namespace when
    one is in scope). An already-expanded Clark token is accepted as an
    internal computed form.
    """
    if value is None:
        return None
    for token in str(value).split():
        if token in (DISALLOWED_DEFINED, DISALLOWED_SIBLING):
            continue
        if token.startswith("##"):
            return token
        if token.startswith("{"):
            continue
        prefix, colon, local = token.partition(":")
        if colon and (not prefix or not local or ":" in local):
            return token
        if resolve_qname is not None:
            try:
                resolve_qname(token)
            except Exception:
                return token
    return None


def not_qname_consistency_problems(
    spec: WildcardSpec, *, target_namespace: str | None = None
) -> list[tuple[str, str]]:
    """Reports ``notQName`` names outside the admitted namespaces.

    XSD 1.1 Wildcard Properties Correct clause 4: the namespace of each
    QName member of {disallowed names} must be allowed by the
    {namespace constraint}. The keyword members are exempt. The check
    reads the namespace constraint only — a name is of course excluded
    by its own ``notQName`` entry, which is not the question here.
    """
    target = spec.effective_target(target_namespace)
    problems: list[tuple[str, str]] = []
    for token in sorted(spec.not_qname):
        match = disallowed_name_parts(token)
        if match is None:
            continue
        uri, _local = match
        if not spec.admits_namespace(uri, target):
            problems.append(
                (
                    "wildcard-invalid",
                    f"notQName name '{token}' lies in a namespace the wildcard's "
                    "namespace constraint does not admit",
                )
            )
    return problems


def wildcard_declaration_problems(
    attributes: Mapping[str, Any],
    *,
    is_attribute: bool,
    resolve_qname: Any = None,
) -> list[tuple[str, str]]:
    """The schema problems in one wildcard's XML attributes.

    Returns ``(code, message)`` pairs for the caller to report:

    * ``wildcard-invalid`` for an illegal namespace-constraint token, a
      ``processContents`` value other than ``skip``/``lax``/``strict``,
      occurrence attributes on ``xs:anyAttribute`` (which is not a
      particle), the XSD 1.1 ``namespace``/``notNamespace`` co-presence,
      or an illegal ``notNamespace``/``notQName`` token (an unknown
      ``##`` keyword, a malformed QName, or a QName whose prefix does
      not resolve);
    * ``invalid-attribute`` for an unqualified XML attribute outside the
      wildcard's allowed set. Qualified attributes (Clark names) are
      foreign and never reported, and namespace declarations are not
      attributes.

    An empty ``namespace`` constraint is not reported: the corpus pins
    such a schema valid (wildZ010), even though it admits nothing.
    ``resolve_qname`` is the optional prefix resolver the 1.1
    ``notQName`` check uses; without it, prefix resolution is skipped
    (the binding layer's own resolution still applies).
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
    raw_not_namespace = attributes.get("notNamespace")
    if raw_namespace is not None and raw_not_namespace is not None:
        problems.append(
            (
                "wildcard-invalid",
                f"<{tag}> must not carry both a namespace and a notNamespace attribute",
            )
        )
    elif raw_not_namespace is not None:
        bad_token = invalid_not_namespace(raw_not_namespace)
        if bad_token is not None:
            problems.append(
                (
                    "wildcard-invalid",
                    f"<{tag}> notNamespace '{raw_not_namespace}' is not legal: "
                    f"'{bad_token}' is not a legal token",
                )
            )
    raw_not_qname = attributes.get("notQName")
    if raw_not_qname is not None:
        bad_token = invalid_not_qname(raw_not_qname, resolve_qname=resolve_qname)
        if bad_token is not None:
            problems.append(
                (
                    "wildcard-invalid",
                    f"<{tag}> notQName '{raw_not_qname}' is not legal: "
                    f"'{bad_token}' is not a QName or a reserved keyword",
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


def replace_wildcard(containing_type: Any, old: WildcardSpec, new: WildcardSpec) -> None:
    """Swaps a registered wildcard spec for its refined form.

    The XSD 1.1 ``notQName`` names can only be expanded once the parsing
    layer's prefix bindings are available, which is after the ER
    constructors have registered their raw spec. The swap replaces the
    same *object* (identity, not equality: an unrelated equal spec must
    not be touched).
    """
    attribute = "wildcardAttributeSpecs" if old.is_attribute else "wildcardElementSpecs"
    specs = getattr(containing_type, attribute, None)
    if not specs:
        return
    setattr(containing_type, attribute, [new if spec is old else spec for spec in specs])


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


def _namespace_constraint(
    spec: WildcardSpec, target_namespace: str | None
) -> tuple[str, frozenset[Any]]:
    """Normalizes a constraint into ``(variety, members)``.

    ``variety`` is ``"any"``, ``"not"`` (``members`` holds the *excluded*
    namespaces) or ``"enum"`` (``members`` holds the admitted ones); the
    absent namespace is the member ``None``. This mirrors the XSD 1.1
    §3.10.2.2 mapping (``##other`` maps to ``not`` with the absent
    namespace and the target namespace in the set) and
    :meth:`WildcardSpec.admits_namespace`, so a computed constraint can
    be re-normalized by a later combination. An empty constraint admits
    nothing.
    """
    if spec.not_namespace:
        target = spec.effective_target(target_namespace)
        members: set[Any] = set()
        for token in spec.not_namespace:
            if token == NAMESPACE_LOCAL:
                members.add(None)
            elif token == NAMESPACE_TARGET:
                members.add(target)
            elif not token.startswith("##"):
                members.add(token)
        return "not", frozenset(members)
    tokens = spec.namespace.split()
    if not tokens:
        return "enum", frozenset()
    if NAMESPACE_ANY in tokens:
        return "any", frozenset()
    if tokens[0] == NAMESPACE_OTHER:
        members = {token for token in tokens[1:] if not token.startswith("##")}
        target = spec.effective_target(target_namespace)
        if target is not None:
            members.add(target)
        if NAMESPACE_LOCAL not in tokens:
            members.add(None)
        return "not", frozenset(members)
    members = {token for token in tokens if not token.startswith("##")}
    if NAMESPACE_TARGET in tokens:
        target = spec.effective_target(target_namespace)
        members.add(target)
    if NAMESPACE_LOCAL in tokens:
        members.add(None)
    return "enum", frozenset(members)


def _namespace_member_allowed(variety: str, members: frozenset[Any], uri: str | None) -> bool:
    """Wildcard allows Namespace Name (XSD 1.1 §3.10.4.3)."""
    if variety == "any":
        return True
    if variety == "not":
        return uri not in members
    return uri in members


def _token_namespace_allowed(variety: str, members: frozenset[Any], token: str) -> bool:
    """Whether a ``notQName`` token's namespace is allowed by a constraint.

    Reserved keywords and unresolved raw tokens name no namespace and are
    never allowed.
    """
    match = disallowed_name_parts(token)
    if match is None:
        return False
    uri, _local = match
    return _namespace_member_allowed(variety, members, uri)


def _token_excluded(token: str, not_qname: frozenset[str]) -> bool:
    """Whether a ``notQName`` set already excludes the token's name."""
    match = disallowed_name_parts(token)
    if match is None:
        return False
    uri, local = match
    for candidate in not_qname:
        other = disallowed_name_parts(candidate)
        if other is None:
            continue
        other_uri, other_local = other
        if other_uri == uri and (other_local is None or other_local == local):
            return True
    return False


def _token_allowed(
    variety: str,
    members: frozenset[Any],
    not_qname: frozenset[str],
    token: str,
) -> bool:
    """Wildcard allows Expanded Name (XSD 1.1 §3.10.4.2) for a token.

    The name is allowed when its namespace is allowed by the constraint
    and the wildcard's disallowed-name set does not contain it.
    """
    if not _token_namespace_allowed(variety, members, token):
        return False
    return not _token_excluded(token, not_qname)


def _intersection_disallowed(
    base: WildcardSpec,
    kind_base: str,
    members_base: frozenset[Any],
    own: WildcardSpec,
    kind_own: str,
    members_own: frozenset[Any],
) -> frozenset[str]:
    """The {disallowed names} of an Attribute Wildcard Intersection.

    §3.10.6.4: each side's QName members survive when the *other* side's
    namespace constraint allows their namespace; the ``##defined`` /
    ``##definedSibling`` keywords survive when either side carries them.
    """
    names: set[str] = set()
    for token in base.not_qname:
        if _token_namespace_allowed(kind_own, members_own, token):
            names.add(token)
    for token in own.not_qname:
        if _token_namespace_allowed(kind_base, members_base, token):
            names.add(token)
    for marker in (DISALLOWED_DEFINED, DISALLOWED_SIBLING):
        if marker in base.not_qname or marker in own.not_qname:
            names.add(marker)
    return frozenset(names)


def _union_disallowed(
    base: WildcardSpec,
    kind_base: str,
    members_base: frozenset[Any],
    own: WildcardSpec,
    kind_own: str,
    members_own: frozenset[Any],
) -> frozenset[str]:
    """The {disallowed names} of an Attribute Wildcard Union.

    §3.10.6.3: each side's QName members survive when the *other*
    wildcard does not allow the name (namespace constraint and disallowed
    names both consulted); a keyword survives only when both sides carry
    it.
    """
    names: set[str] = set()
    for token in base.not_qname:
        if disallowed_name_parts(token) is None:
            continue
        if not _token_allowed(kind_own, members_own, own.not_qname, token):
            names.add(token)
    for token in own.not_qname:
        if disallowed_name_parts(token) is None:
            continue
        if not _token_allowed(kind_base, members_base, base.not_qname, token):
            names.add(token)
    for marker in (DISALLOWED_DEFINED, DISALLOWED_SIBLING):
        if marker in base.not_qname and marker in own.not_qname:
            names.add(marker)
    return frozenset(names)


def _combine_namespace(
    variety: str,
    members: frozenset[Any],
    base: WildcardSpec,
    own: WildcardSpec,
    target_namespace: str | None,
) -> tuple[str, str | None, frozenset[str]]:
    """Encodes a computed constraint as ``(namespace, target, not_namespace)``.

    ``"any"`` and ``"enum"`` results are written into the ``namespace``
    string; a ``"not"`` result is written as the classic ``##other``
    spelling when one of the source targets is among the excluded
    namespaces, and otherwise as literal exclusions with no target (so
    the ``##other`` target does not over-exclude). The absent namespace
    is written as ``##local`` when it is admitted, the same convention
    the existing combination code uses.
    """
    if variety == "any":
        return NAMESPACE_ANY, target_namespace, frozenset()
    if variety == "enum":
        tokens = sorted(uri for uri in members if uri is not None)
        if None in members:
            tokens.append(NAMESPACE_LOCAL)
        return " ".join(tokens), target_namespace, frozenset()
    present = {uri for uri in members if uri is not None}
    absent_excluded = None in members
    target: str | None = None
    for spec in (base, own):
        candidate = spec.effective_target(target_namespace)
        if candidate is not None and candidate in present:
            target = candidate
            break
    if target is None and target_namespace is not None and target_namespace in present:
        target = target_namespace
    extras = sorted(present - {target}) if target is not None else sorted(present)
    if not extras and not present and not absent_excluded:
        return NAMESPACE_ANY, target_namespace, frozenset()
    if not present and absent_excluded:
        # Only the absent namespace is excluded: "every present
        # namespace, not absent" is exactly a targetless ##other.
        return NAMESPACE_OTHER, None, frozenset()
    namespace = NAMESPACE_OTHER if not extras else f"{NAMESPACE_OTHER} {' '.join(extras)}"
    if not absent_excluded:
        namespace = f"{namespace} {NAMESPACE_LOCAL}"
    return namespace, target, frozenset()


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
    *,
    not_namespace: frozenset[str] = frozenset(),
    not_qname: frozenset[str] = frozenset(),
) -> WildcardSpec:
    """Builds a computed spec from two source specs."""
    return WildcardSpec(
        namespace=namespace,
        process_contents=_process_contents_name(severity),
        is_attribute=base.is_attribute or own.is_attribute,
        target_namespace=target_namespace,
        not_namespace=not_namespace,
        not_qname=not_qname,
    )


def intersect_wildcard_specs(
    base: WildcardSpec,
    own: WildcardSpec,
    target_namespace: str | None = None,
) -> WildcardSpec:
    """The intersection of two attribute-wildcard constraints.

    Namespace constraints intersect as XSD 1.1 §3.10.6.4 defines it
    (errata E1-10): ``##any`` yields the other side, enumerations
    intersect, two ``not`` constraints union their exclusions, and a
    ``not`` against an enumeration keeps the enumerated namespaces the
    ``not`` does not exclude. The absent namespace is admitted only when
    *both* constraints admit it.

    The {disallowed names} set is computed by the intersection rule:
    each side's QName members survive when the other side's namespace
    constraint allows their namespace, and ``##defined`` /
    ``##definedSibling`` survive when either side carries them. The
    intersection takes the *weaker* ``processContents`` of the two
    (``skip`` < ``lax`` < ``strict``); the caller decides whether a
    derived type may weaken its base by comparing severities.
    """
    kind_base, members_base = _namespace_constraint(base, target_namespace)
    kind_own, members_own = _namespace_constraint(own, target_namespace)
    severity = min(_severity(base.process_contents), _severity(own.process_contents))
    disallowed = _intersection_disallowed(base, kind_base, members_base, own, kind_own, members_own)
    if kind_base == "any" and kind_own == "any":
        return _combine(base, own, NAMESPACE_ANY, severity, target_namespace, not_qname=disallowed)
    if kind_base == "any":
        return _combine(
            base,
            own,
            own.namespace,
            severity,
            own.target_namespace,
            not_namespace=own.not_namespace,
            not_qname=disallowed,
        )
    if kind_own == "any":
        return _combine(
            base,
            own,
            base.namespace,
            severity,
            base.target_namespace,
            not_namespace=base.not_namespace,
            not_qname=disallowed,
        )
    if kind_base == "enum" and kind_own == "enum":
        variety, members = "enum", members_base & members_own
    elif kind_base == "not" and kind_own == "not":
        variety, members = "not", members_base | members_own
    elif kind_base == "not":
        variety, members = "enum", members_own - members_base
    else:
        variety, members = "enum", members_base - members_own
    namespace, target, not_namespace = _combine_namespace(
        variety, members, base, own, target_namespace
    )
    return _combine(
        base,
        own,
        namespace,
        severity,
        target,
        not_namespace=not_namespace,
        not_qname=disallowed,
    )


def union_wildcard_specs(
    base: WildcardSpec,
    own: WildcardSpec,
    target_namespace: str | None = None,
) -> WildcardSpec:
    """The union of two attribute-wildcard constraints (extension).

    Both wildcards apply to the derived type, so the union admits every
    namespace either side admits (XSD 1.1 §3.10.6.3, errata E1-10):
    ``##any`` wins, enumerations union, two ``not`` constraints exclude
    only the intersection of their exclusions (the constraint is ``any``
    when that intersection is empty), and a ``not`` against an
    enumeration excludes the set difference. The union takes the
    *stronger* ``processContents``: a single combined spec must not
    weaken an obligation that either source wildcard imposes.

    The {disallowed names} set is computed by the union rule: each
    side's QName members survive when the *other wildcard* (namespace
    constraint and disallowed names together) does not allow the name; a
    keyword survives only when both sides carry it.
    """
    kind_base, members_base = _namespace_constraint(base, target_namespace)
    kind_own, members_own = _namespace_constraint(own, target_namespace)
    severity = max(_severity(base.process_contents), _severity(own.process_contents))
    disallowed = _union_disallowed(base, kind_base, members_base, own, kind_own, members_own)
    members: frozenset[Any]
    if kind_base == "any" or kind_own == "any":
        variety, members = "any", frozenset()
    elif kind_base == "enum" and kind_own == "enum":
        variety, members = "enum", members_base | members_own
    elif kind_base == "not" and kind_own == "not":
        members = members_base & members_own
        variety = "not" if members else "any"
    else:
        not_members = members_base if kind_base == "not" else members_own
        enum_members = members_own if kind_base == "not" else members_base
        members = not_members - enum_members
        variety = "not" if members else "any"
    namespace, target, not_namespace = _combine_namespace(
        variety, members, base, own, target_namespace
    )
    return _combine(
        base,
        own,
        namespace,
        severity,
        target,
        not_namespace=not_namespace,
        not_qname=disallowed,
    )


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
    "DISALLOWED_DEFINED",
    "DISALLOWED_SIBLING",
    "NAMESPACE_ANY",
    "NAMESPACE_LOCAL",
    "NAMESPACE_OTHER",
    "NAMESPACE_TARGET",
    "PROCESS_CONTENTS",
    "PROCESS_SEVERITY",
    "WildcardSpec",
    "disallowed_name_parts",
    "effective_attribute_wildcard",
    "excluded_locals",
    "excludes_all_locals",
    "intersect_wildcard_specs",
    "invalid_namespace_constraint",
    "invalid_not_namespace",
    "invalid_not_qname",
    "invalid_process_contents",
    "not_qname_consistency_problems",
    "register_wildcard",
    "replace_wildcard",
    "split_name",
    "union_wildcard_specs",
    "wildcard_declaration_problems",
    "wildcard_spec",
    "wildcard_specs_overlap",
]
