"""The XSD 1.1 selector/field XPath subset: parse, validate, translate.

Identity-constraint ``xs:selector`` and ``xs:field`` paths are not full
XPath: XSD 1.1 restricts them to a small grammar (§3.11.6.2/3), with the
W3C test suite as the arbiter wherever the spec text leaves room. The
pipeline is:

1. **parse** with :class:`elementpath.XPath1Parser`, passing the
   declaration-site prefix bindings (a syntax error or an unbound
   prefix therefore fails at schema phase — everything in the subset is
   XPath 1.0, so a parse failure is always a subset violation);
2. **whitelist** the AST: only the node shapes the subset admits are
   accepted, and *any unrecognized AST node type is rejected* — never
   silently accepted — so an elementpath release that reshapes its AST
   fails loudly instead of mis-parsing;
3. **translate** to a :class:`ParsedXPath` of step tuples, resolving
   unprefixed element names through the XPath default namespace (an
   XSD concept elementpath knows nothing about).

The admitted subset, as arbitrated by the XSTS corpus (idI*/idJ*/idL*,
XPathDefaultNSonKeyKeyRefUnique) under XSD 1.1 §3.11.6:

- a top-level union of paths (``a | @b``), in selectors and fields;
- an optional leading descendant-or-self prefix ``.//`` (the grammar
  requires the ``.`` token: ``Path ::= ('.' '//')? Step ...``) — never
  in the middle of a path and never as a bare ``//``;
- steps ``.``, ``qname``, ``prefix:*``, ``*``, full child-axis syntax
  (``child::qname``, idI009), full attribute-axis syntax
  (``attribute::qname``, idL017) and abbreviated attribute steps
  (``@qname``, ``@prefix:*``, ``@*``) — an attribute step is only
  allowed as the final step of a path;
- free whitespace between tokens; the elementpath lexer already
  rejects the corpus-illegal forms such as ``prefix :*``.

Rejected, with corpus ids: predicates (idI152), absolute paths
(idI003, idD014), bare ``//`` (idI004), ``self::``/``descendant::``/
``descendant-or-self::`` axes (idI145-150, idJ205-210), function calls
(idE014) and mid-path ``//`` (idI028, idJ052).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from elementpath import XPath1Parser
from elementpath.exceptions import ElementPathError

from pyxsd.exceptions import XPathError  # re-exported for compatibility
from pyxsd.namespaces import XML_NS, clark

#: A translated step: ``('self',)``, ``('attribute', name-or-'*')`` or
#: ``('element', name-or-'*')``. A name is Clark (``{uri}local``) or a
#: bare local name for the no namespace.
Step = tuple[str, ...]

#: One union alternative: ``(descendant_or_self_prefix, steps)``.
Path = tuple[bool, tuple[Step, ...]]


class ParsedXPath(NamedTuple):
    """A validated selector/field path, ready for evaluation."""

    #: The union alternatives, in source order.
    alternatives: tuple[Path, ...]


#: elementpath AST node names for the admitted shapes. Name-matched on
#: purpose: the classes are private to elementpath, so matching the
#: (stable across 4.7-5.x) names is the version-drift guard.
_CONTEXT_ITEM = "ContextItemToken"
_NAME_TEST = "NameToken"
_PREFIXED_NAME_TEST = "PrefixedNameToken"
_WILDCARD = "AsteriskToken"
_UNION = "_VerticalLineOperator"
_SLASH = "_SolidusOperator"
_SLASH_SLASH = "_SolidusSolidusOperator"
_CHILD_STEP = "_ChildSymbol"
_ATTRIBUTE_STEP = ("_CommercialAtAttributeReference", "_AttributeSymbol")


def parse_xpath_subset(
    path: str,
    namespaces: dict[str, str],
    default_ns: str | None,
    target_ns: str | None,
) -> ParsedXPath:
    """Parses one selector/field path under its declaration context.

    - ``path``: the raw ``xpath`` attribute value.
    - ``namespaces``: the in-scope prefix bindings at the declaration
      site (the default namespace is excluded; unprefixed names are
      resolved through ``default_ns`` instead).
    - ``default_ns``: the namespace unprefixed *element* names resolve
      to — the caller resolves the ``xpathDefaultNamespace`` keyword
      forms first (unprefixed attribute names are always in no
      namespace).
    - ``target_ns``: the schema's target namespace. Accepted so the
      declaration context travels with the call, but the body does not
      consume it: callers resolve the ``xpathDefaultNamespace`` keyword
      forms (including ``##targetNamespace``) before invoking.

    Raises :class:`XPathError` when the expression is outside the
    subset (or not XPath at all).
    """
    text = (path or "").strip()
    if not text:
        raise XPathError("the xpath expression is empty")
    bindings = {prefix: uri for prefix, uri in (namespaces or {}).items() if prefix}
    # The ``xml`` prefix is bound by the XML specification itself.
    bindings.setdefault("xml", XML_NS)
    try:
        tree = XPath1Parser(namespaces=bindings).parse(text)
    except ElementPathError as exc:
        raise XPathError(f"not a valid XPath expression: {exc}") from exc
    return ParsedXPath(_alternatives(tree, default_ns))


def _alternatives(node: Any, default_ns: str | None) -> tuple[Path, ...]:
    """Splits a top-level union into its alternatives."""
    kind = type(node).__name__
    if kind == _UNION:
        children = list(node)
        if len(children) != 2:
            raise XPathError("malformed union expression")
        return (
            *_alternatives(children[0], default_ns),
            *_alternatives(children[1], default_ns),
        )
    return (_alternative(node, default_ns),)


def _alternative(node: Any, default_ns: str | None) -> Path:
    """Translates one alternative: a (possibly empty) leading ``.//``
    prefix and a sequence of child/attribute/``.`` steps."""
    steps: list[Step] = []
    descendant = _walk(node, steps, leading=True, default_ns=default_ns)
    for index, step in enumerate(steps):
        if step[0] == "attribute" and index != len(steps) - 1:
            raise XPathError("an attribute step is only allowed as the final step of a path")
    return (descendant, tuple(steps))


def _walk(
    node: Any,
    steps: list[Step],
    *,
    leading: bool,
    default_ns: str | None,
) -> bool:
    """Appends the steps of one path subtree, in document order.

    Returns whether the subtree establishes the descendant-or-self
    prefix. Operator nodes are matched by name; anything the subset
    does not admit raises :class:`XPathError`.
    """
    kind = type(node).__name__
    children = list(node)
    if kind == _SLASH:
        # A binary slash is a step separator; a unary one is an
        # absolute path (``/a``), which the subset forbids.
        if len(children) != 2:
            raise XPathError("absolute paths are outside the identity-constraint XPath subset")
        descendant = _walk(children[0], steps, leading=leading, default_ns=default_ns)
        _walk(children[1], steps, leading=False, default_ns=default_ns)
        return descendant
    if kind == _SLASH_SLASH:
        # Only the leading descendant-or-self prefix ``.//x`` is
        # admitted: the grammar requires the ``.`` token
        # (``Path ::= ('.' '//')? Step ...``), so a bare ``//x`` is a
        # subset violation (idI004).
        if leading and len(children) == 2 and type(children[0]).__name__ == _CONTEXT_ITEM:
            _walk(children[1], steps, leading=False, default_ns=default_ns)
            return True
        raise XPathError("'//' is only allowed as the leading './/' of a path")
    if kind == _UNION:
        raise XPathError("unions are allowed only at the top level of the expression")
    steps.append(_step(node, children, default_ns))
    return False


def _step(node: Any, children: list[object], default_ns: str | None) -> Step:
    """Translates one admitted step node."""
    kind = type(node).__name__
    if kind == _CONTEXT_ITEM:
        return ("self",)
    if kind in _ATTRIBUTE_STEP:
        if len(children) != 1:
            raise XPathError(f"{kind} is outside the identity-constraint XPath subset")
        return ("attribute", _attribute_name(children[0]))
    if kind == _CHILD_STEP:
        if len(children) != 1:
            raise XPathError(f"{kind} is outside the identity-constraint XPath subset")
        return _element_step(children[0], default_ns)
    return _element_step(node, default_ns)


def _element_step(node: Any, default_ns: str | None) -> Step:
    """Translates one name test to an element step.

    An unprefixed name resolves through ``default_ns`` (the resolved
    XPath default namespace); a prefixed name arrives already expanded
    in Clark form from elementpath. Unprefixed ``*`` stays bare.
    """
    kind = type(node).__name__
    if kind == _NAME_TEST:
        return ("element", clark(default_ns, node.value))
    if kind == _WILDCARD:
        return ("element", "*")
    if kind == _PREFIXED_NAME_TEST:
        return ("element", node.name)
    raise XPathError(f"{kind} is outside the identity-constraint XPath subset")


def _attribute_name(node: Any) -> str:
    """Translates one name test to an attribute name.

    Unprefixed attributes are always in no namespace, so an unprefixed
    name stays bare regardless of the XPath default namespace.
    """
    kind = type(node).__name__
    if kind == _NAME_TEST:
        return node.value
    if kind == _WILDCARD:
        return "*"
    if kind == _PREFIXED_NAME_TEST:
        return node.name
    raise XPathError(f"{kind} is not a valid attribute step")
