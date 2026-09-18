"""The XSD 1.1 assertion and conditional-type-assignment XPath 2.0 subsets.

Both constructs carry their ``test`` as an XPath 2.0 expression, but the
two subsets differ sharply. ``xs:assert``/``xs:assertion``
(§3.13.1) is a general XPath 2.0 expression over the element being
validated: child and descendant navigation, predicates, the full
operator/function library and ``$value`` are all in. The function
library is restricted — no ``fn:doc``/``fn:collection``/``fn:resolve-uri``
and friends, and no namespace axis. Conditional type assignment
(§3.3.2.1) is far narrower: a boolean combination of attribute tests on
the element itself, expressed with ``@name`` references and literals.

The pipeline mirrors :mod:`pyxsd.xpath_subset`:

1. **parse** with :class:`elementpath.XPath2Parser`, passing the
   declaration-site prefix bindings, so a syntax error or an unbound
   prefix fails at schema phase;
2. **whitelist** the AST with a fail-loud visitor — an unrecognized node
   type is rejected, never silently accepted, so an elementpath release
   that reshapes its AST fails loudly instead of mis-parsing; the
   assertion visitor additionally denies the resource functions and the
   namespace axis, while the CTA visitor admits only attribute/literal
   boolean trees;
3. **evaluate** through elementpath's XPath 2.0 evaluator against a
   *plain* :mod:`xml.etree.ElementTree` node (never a pyxsd bound
   object), mapping evaluator errors to :class:`XPathError`.

Do not register a custom tree builder: elementpath 5 removed public
custom-tree registration and bound objects expose their underlying
ElementTree node, which is what callers pass here.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from elementpath import XPath2Parser, XPathContext
from elementpath.exceptions import ElementPathError
from elementpath.xpath_tokens import XPathAxis, XPathFunction

from pyxsd.namespaces import XML_NS
from pyxsd.xpath_subset import XPathError

__all__ = [
    "CompiledXPath",
    "evaluate",
    "parse_assertion_xpath",
    "parse_cta_xpath",
]


class CompiledXPath(NamedTuple):
    """A parsed, subset-validated assertion or CTA expression."""

    #: The raw ``test`` attribute value (stripped).
    text: str
    #: The in-scope prefix bindings handed to the parser (with ``xml``).
    namespaces: dict[str, str]
    #: The elementpath parse tree, evaluated by :func:`evaluate`.
    tree: Any


#: Functions the assertion subset forbids because they reach outside the
#: instance being validated (§3.13.1). An unregistered function already
#: fails at parse time, so this set only needs the reachable names.
_ASSERTION_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "doc",
        "doc-available",
        "collection",
        "uri-collection",
        "resolve-uri",
        "unparsed-text",
        "unparsed-text-lines",
        "unparsed-text-available",
    }
)

#: The namespace axis is not part of the assertion data model.
_ASSERTION_FORBIDDEN_AXES = frozenset({"namespace"})

#: elementpath AST class names admitted by the assertion subset: literals,
#: name/context tokens and the XPath 2.0 operators/expressions. Functions
#: and axes are classified separately by base class above. Delimiters and
#: parser boundary symbols are deliberately absent — they must never be
#: the root of a parsed expression, and admitting them would weaken the
#: fail-loud guarantee.
_ASSERTION_STRUCTURAL = frozenset(
    {
        "ValueToken",
        "NameToken",
        "PrefixedNameToken",
        "BracedNameToken",
        "VariableToken",
        "AsteriskToken",
        "ContextItemToken",
        "ParentShortcutToken",
        "_StringLiteral",
        "_FloatLiteral",
        "_DecimalLiteral",
        "_IntegerLiteral",
        "_OrOperator",
        "_AndOperator",
        "_EqualsSignOperator",
        "_ExclamationMarkEqualsSignOperator",
        "_LessThanSignOperator",
        "_GreaterThanSignOperator",
        "_LessThanSignEqualsSignOperator",
        "_GreaterThanSignEqualsSignOperator",
        "_PlusSignOperator",
        "_HyphenMinusOperator",
        "_DivOperator",
        "_ModOperator",
        "_IdivOperator",
        "_VerticalLineOperator",
        "_SolidusSolidusOperator",
        "_SolidusOperator",
        "_LeftSquareBracketOperator",
        "_CommercialAtAttributeReference",
        "_CommaOperator",
        "_LeftParenthesisExpression",
        "_EqOperator",
        "_NeOperator",
        "_LtOperator",
        "_GtOperator",
        "_LeOperator",
        "_GeOperator",
        "_IsOperator",
        "_LessThanSignLessThanSignOperator",
        "_GreaterThanSignGreaterThanSignOperator",
        "_ToOperator",
        "_InstanceExpression",
        "_TreatExpression",
        "_CastableExpression",
        "_CastExpression",
        "_IfExpression",
        "_ForExpression",
        "_SomeExpression",
        "_EveryExpression",
        "_ReturnOperator",
        "_InOperator",
        "_ThenOperator",
        "_ElseOperator",
        "_AsOperator",
        "_OfOperator",
        "_SatisfiesOperator",
        "_UnionSymbol",
        "_IntersectOperator",
        "_ExceptOperator",
        "_QuestionMarkSymbol",
        "_AttributeKind_Test__Axis",
    }
)

#: Binary boolean/relational operators admitted by the CTA subset.
_CTA_OPERATORS = frozenset(
    {
        "_EqualsSignOperator",
        "_ExclamationMarkEqualsSignOperator",
        "_LessThanSignOperator",
        "_GreaterThanSignOperator",
        "_LessThanSignEqualsSignOperator",
        "_GreaterThanSignEqualsSignOperator",
        "_EqOperator",
        "_NeOperator",
        "_LtOperator",
        "_GtOperator",
        "_LeOperator",
        "_GeOperator",
        "_AndOperator",
        "_OrOperator",
    }
)

#: Literal operands admitted by the CTA subset.
_CTA_LITERALS = frozenset({"_StringLiteral", "_IntegerLiteral", "_DecimalLiteral", "_FloatLiteral"})

#: Name-node types that may follow ``@`` in the CTA subset.
_CTA_ATTRIBUTE_NAMES = frozenset({"NameToken", "PrefixedNameToken"})


class _AssertionChecker:
    """Fail-loud visitor for the XSD 1.1 assertion XPath 2.0 subset."""

    def check(self, node: Any) -> None:
        self._visit(node)

    def _visit(self, node: Any) -> None:
        if isinstance(node, XPathFunction):
            if getattr(node, "symbol", None) in _ASSERTION_FORBIDDEN_FUNCTIONS:
                raise XPathError(f"fn:{node.symbol} is not allowed in an assertion test")
        elif isinstance(node, XPathAxis):
            if getattr(node, "symbol", None) in _ASSERTION_FORBIDDEN_AXES:
                raise XPathError("the namespace axis is not allowed in an assertion test")
        elif type(node).__name__ not in _ASSERTION_STRUCTURAL:
            raise XPathError(f"{type(node).__name__} is outside the assertion XPath subset")
        for child in node:
            self._visit(child)


class _CTAChecker:
    """Fail-loud visitor for the conditional-type-assignment subset."""

    def check(self, node: Any) -> None:
        self._visit(node)

    def _visit(self, node: Any) -> None:
        kind = type(node).__name__
        if isinstance(node, XPathFunction):
            if kind != "_NotFunction":
                raise XPathError(
                    f"{getattr(node, 'symbol', kind)} is outside the "
                    "conditional-type-assignment XPath subset"
                )
            children = list(node)
            if len(children) != 1:
                raise XPathError("malformed not() in a type alternative test")
            self._visit(children[0])
            return
        if kind == "_CommercialAtAttributeReference":
            children = list(node)
            if len(children) != 1 or type(children[0]).__name__ not in _CTA_ATTRIBUTE_NAMES:
                raise XPathError("malformed attribute test in a type alternative")
            return
        if kind in _CTA_OPERATORS:
            children = list(node)
            if len(children) != 2:
                raise XPathError(f"malformed {kind} in a type alternative test")
            self._visit(children[0])
            self._visit(children[1])
            return
        if kind == "_LeftParenthesisExpression":
            children = list(node)
            if len(children) != 1:
                raise XPathError("malformed parenthesized type alternative test")
            self._visit(children[0])
            return
        if kind in _CTA_LITERALS:
            return
        raise XPathError(f"{kind} is outside the conditional-type-assignment XPath subset")


def parse_assertion_xpath(text: str, namespaces: dict[str, str]) -> CompiledXPath:
    """Parses an ``xs:assert``/``xs:assertion`` ``test`` expression.

    Raises :class:`pyxsd.xpath_subset.XPathError` when the expression is
    empty, uses an unbound prefix, is not valid XPath 2.0, or contains a
    construct outside the assertion subset (a forbidden resource
    function, the namespace axis, or an unrecognized AST node).
    """
    return _parse(text, namespaces, _AssertionChecker())


def parse_cta_xpath(text: str, namespaces: dict[str, str]) -> CompiledXPath:
    """Parses an ``xs:alternative`` ``test`` expression.

    The CTA subset is attribute tests on the element itself: ``@name``
    references, literals, value/like comparisons, ``and``/``or`` and
    ``not``. Child/descendant/parent navigation, predicates, context-item
    access and every other function are rejected with
    :class:`pyxsd.xpath_subset.XPathError`.
    """
    return _parse(text, namespaces, _CTAChecker())


def _parse(
    text: str,
    namespaces: dict[str, str],
    checker: _AssertionChecker | _CTAChecker,
) -> CompiledXPath:
    source = (text or "").strip()
    if not source:
        raise XPathError("the xpath expression is empty")
    bindings = {prefix: uri for prefix, uri in (namespaces or {}).items() if prefix}
    bindings.setdefault("xml", XML_NS)
    try:
        tree = XPath2Parser(namespaces=bindings).parse(source)
    except (ElementPathError, TypeError, ValueError) as exc:
        raise XPathError(f"not a valid XPath expression: {exc}") from exc
    checker.check(tree)
    return CompiledXPath(source, bindings, tree)


def evaluate(
    compiled: Any,
    node: Any,
    *,
    value: Any | None = None,
    variable_values: dict[str, Any] | None = None,
    variable_types: dict[str, Any] | None = None,
) -> Any:
    """Evaluates a compiled assertion/CTA expression against ``node``.

    ``node`` is a plain :class:`xml.etree.ElementTree.Element` (the context
    item). ``value`` binds ``$value`` for simple-type assertions; it takes
    precedence over a ``value`` key in ``variable_values``. The raw XPath
    result is returned — the caller coerces truthiness. ``variable_types``
    is accepted for interface symmetry with the schema phase, where
    elementpath consumes variable types at parse time.

    Any evaluator failure (``ElementPathError``, ``TypeError``,
    ``ValueError``) is mapped to :class:`XPathError`.
    """
    tree = compiled.tree if isinstance(compiled, CompiledXPath) else compiled
    variables = dict(variable_values or {})
    if value is not None:
        variables["value"] = value
    try:
        context = XPathContext(root=node, variables=variables or None)
        return tree.evaluate(context)
    except (ElementPathError, TypeError, ValueError) as exc:
        raise XPathError(f"XPath evaluation failed: {exc}") from exc
