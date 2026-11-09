"""Independent evaluation of W3C XSLT test-catalog result assertions.

The engine under test returns a :class:`ResultSnapshot` (or a raw value); it
never classifies its own result.  This module parses the catalog's nested
assertion vocabulary, evaluates it, and raises one of three explicit harness
signals when the assertion cannot be decided:

``AssertionGap``
    The assertion family is not implemented or the result does not carry the
    data needed to evaluate it.  The runner reports ``adapter-gap``.

``AssertionDispute``
    The catalog's expectation cannot be interpreted (missing file, malformed
    expected XML, unresolvable error-code prefix, non-numeric count).  The
    runner reports ``corpus-dispute``.

``AssertionFailure``
    The actual result cannot be interpreted (for example malformed XML).  The
    runner reports ``fail``.

XPath assertions use elementpath as a shared substrate.  No XSLT engine code
participates in the verdict.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, NoReturn

from elementpath import (
    DocumentNode,
    ElementNode,
    UnsupportedFeatureError,
    XPathContext,
    XPathNode,
    build_node_tree,
)
from elementpath.exceptions import ElementPathSyntaxError
from elementpath.xpath3 import XPath31Parser
from elementpath.xpath_tokens import XPathArray, XPathMap
from tests.xslt.model import XML_NS, TestCase
from tests.xslt.outcomes import EXPECTED_ERROR_FAMILY, CaseResult

#: Assertion-family names recorded as executed.  These name assertion
#: capabilities, never catalog cases.
XPATH_FAMILY = "xpath"
XML_FAMILY = "xml"
STRING_FAMILY = "string"
COUNT_FAMILY = "count"
TYPE_FAMILY = "type"
EMPTY_FAMILY = "empty"
BOOLEAN_FAMILY = "boolean"
VALUE_FAMILY = "value"
SERIALIZATION_FAMILY = "serialization"
MESSAGE_FAMILY = "message"
SECONDARY_FAMILY = "secondary"
WARNING_FAMILY = "warning"

#: XPath error-code namespace.
XQT_ERRORS_NS = "http://www.w3.org/2005/xqt-errors"

#: Standard prefixes available to assertion expressions even when the catalog
#: does not declare them.  Catalog declarations override these entries.
DEFAULT_NAMESPACES: Mapping[str, str] = {
    "xs": "http://www.w3.org/2001/XMLSchema",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "fn": "http://www.w3.org/2005/xpath-functions",
    "map": "http://www.w3.org/2005/xpath-functions/map",
    "array": "http://www.w3.org/2005/xpath-functions/array",
    "math": "http://www.w3.org/2005/xpath-functions/math",
    "err": XQT_ERRORS_NS,
    "xml": XML_NS,
    "xsl": "http://www.w3.org/1999/XSL/Transform",
}

#: Static XPath errors that mean the harness cannot evaluate the family yet.
_GAP_CODES = frozenset(
    {
        "XPST0003",
        "XPST0008",
        "XPST0017",
        "XPST0051",
        "XPST0081",
        "XQST0031",
        "XQST0059",
        "FOAP0001",
    }
)

_CATALOG_NS = "http://www.w3.org/2012/10/xslt-test-catalog"
_WRAPPER = "pyxsd-assertion-wrapper"
_MISSING = object()


class AssertionGap(Exception):
    """The harness cannot execute this assertion family."""


class AssertionDispute(Exception):
    """The catalog expectation is unusable or contradictory."""


class AssertionFailure(Exception):
    """The actual result cannot be interpreted by this assertion."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _in_catalog_namespace(tag: str) -> bool:
    return not tag.startswith("{") or tag.startswith(f"{{{_CATALOG_NS}}}")


def _namespace_map(declarations: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Merge standard prefixes with one assertion's in-scope declarations.

    The default namespace is deliberately dropped: an unprefixed name in an
    ``assert`` expression is in no namespace, matching the catalog schema.
    """
    namespaces = dict(DEFAULT_NAMESPACES)
    namespaces.update((prefix, uri) for prefix, uri in declarations if prefix)
    return namespaces


def _optional_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("true", "1")


@dataclass(frozen=True)
class ResultSnapshot:
    """Everything an assertion may inspect about one transformation result.

    ``raw`` is the exact principal sequence.  ``xml`` is the principal document
    serialization when the result is a document node; when ``raw`` is empty and
    ``xml`` is present, the principal result is treated as one document node.
    ``messages`` and ``secondary`` use ``None`` to mean "the executor did not
    provide this data", which is an adapter gap rather than an empty result.
    """

    raw: tuple[Any, ...] = ()
    xml: str | None = None
    serialized: str | None = None
    messages: tuple[ResultSnapshot, ...] | None = None
    secondary: Mapping[str, ResultSnapshot] | None = None
    warnings: tuple[str, ...] | None = None
    error_code: str | None = None
    error_message: str = ""
    serialization_error_code: str | None = None
    serialization_error_message: str = ""
    serialization_method: str | None = None
    serialization_encoding: str | None = None
    serialization_bom: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", tuple(self.raw))


@dataclass
class AssertionContext:
    """Evaluation state shared by nested assertions."""

    result: ResultSnapshot
    case: TestCase | None = None
    error_mode: bool = False
    coverage: set[str] = field(default_factory=set)

    def record(self, family: str) -> None:
        self.coverage.add(family)

    def for_result(self, result: ResultSnapshot) -> AssertionContext:
        return AssertionContext(result, self.case, self.error_mode, self.coverage)


@dataclass(frozen=True)
class Verdict:
    """A decided assertion: pass, fail, or a strict wrong-error outcome."""

    status: str
    expected: str | None = None
    actual: str | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def _passed() -> Verdict:
    return Verdict("pass")


def _failed(detail: str = "") -> Verdict:
    return Verdict("fail", detail=detail)


def _wrong(expected: str, actual: str) -> Verdict:
    return Verdict("wrong-error", expected=expected, actual=actual)


class Assertion:
    """Base class for one catalog assertion element."""

    family: ClassVar[str | None] = None

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# XPath and raw-result helpers


def _raw_items(result: ResultSnapshot) -> tuple[Any, ...]:
    if result.raw:
        return result.raw
    if result.xml is not None:
        return (_document_node(result.xml),)
    return ()


def _parse_actual_xml(xml: str) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        try:
            return ET.fromstring(f"<{_WRAPPER}>{xml}</{_WRAPPER}>")
        except ET.ParseError as exc:
            raise AssertionFailure(f"actual XML result is not well formed: {exc}") from exc


def _document_node(xml: str) -> Any:
    if not xml.strip():
        return build_node_tree(ET.ElementTree())
    return build_node_tree(ET.ElementTree(_parse_actual_xml(xml)))


def _xpath_value(value: Any, parser: XPath31Parser) -> Any:
    if isinstance(value, (XPathMap, XPathArray)):
        return value
    if isinstance(value, ET.ElementTree):
        return build_node_tree(value)
    if isinstance(value, ET.Element):
        return build_node_tree(value)
    if isinstance(value, Mapping):
        items = [
            (_xpath_value(key, parser), _xpath_value(item, parser)) for key, item in value.items()
        ]
        return XPathMap(parser, items)
    if isinstance(value, list):
        return XPathArray(parser, [_xpath_value(item, parser) for item in value])
    if isinstance(value, tuple):
        return XPathArray(parser, [_xpath_value(item, parser) for item in value])
    return value


_STEP_SYMBOLS = frozenset(
    {
        "(name)",
        "@",
        "attribute",
        "child",
        "descendant",
        "self",
        "parent",
        "ancestor",
        "ancestor-or-self",
        "following",
        "following-sibling",
        "preceding",
        "preceding-sibling",
        "text",
        "node",
        "comment",
        "processing-instruction",
    }
)

_TYPE_PARENT_SYMBOLS = frozenset(
    {
        ":",
        "array",
        "map",
        "function",
        "element",
        "attribute",
        "document-node",
        "schema-element",
        "schema-attribute",
        "comment",
        "text",
        "node",
        "processing-instruction",
        "empty-sequence",
        "item",
        "instance",
        "treat",
        "cast",
        "castable",
        "as",
    }
)


def _is_step(token: Any) -> bool:
    symbol = getattr(token, "symbol", None)
    return symbol in _STEP_SYMBOLS or symbol in {".", ".."} or (symbol == "*" and len(token) == 0)


def _context_node_path(ast: Any) -> bool:
    """Whether an expression has a step that needs the context item as a node.

    A bare ``.`` is valid with any context item, and a variable-rooted path
    such as ``$result/a`` does not depend on the assertion's context item.
    Relative name/axis steps at the top level, ``./a``/``.//a``, and
    context-rooted absolute paths do require a node context.  Predicate focus
    is local to the filtered sequence, so a step inside a predicate is not an
    outer-context dependency.
    """

    def walk(
        token: Any,
        *,
        local_focus: bool,
        rooted_step: bool = False,
        parent: Any | None = None,
    ) -> bool:
        symbol = getattr(token, "symbol", None)
        type_context = (
            parent is not None and getattr(parent, "symbol", None) in _TYPE_PARENT_SYMBOLS
        )
        if symbol in ("/", "//"):
            if len(token) < 2:
                return not local_focus
            left, right = token[0], token[1]
            if _is_step(left):
                if not local_focus:
                    return not type_context
                return walk(left, local_focus=True, rooted_step=True, parent=token) or walk(
                    right, local_focus=True, rooted_step=True, parent=token
                )
            return walk(left, local_focus=local_focus, parent=token) or walk(
                right, local_focus=local_focus, rooted_step=True, parent=token
            )
        if symbol == "[" and len(token) == 2:
            return walk(
                token[0], local_focus=local_focus, rooted_step=rooted_step, parent=token
            ) or walk(token[1], local_focus=True, parent=token)
        if symbol == "[":
            return any(
                walk(
                    child,
                    local_focus=local_focus,
                    rooted_step=rooted_step,
                    parent=token,
                )
                for child in token
            )
        if symbol == ".":
            return False
        if symbol == "$":
            return False
        if symbol == "(name)":
            if type_context or (parent is not None and getattr(parent, "symbol", None) == "$"):
                return False
            return not (rooted_step or local_focus)
        if _is_step(token):
            if type_context:
                return False
            return not (rooted_step or local_focus)
        return any(
            walk(
                child,
                local_focus=local_focus,
                rooted_step=rooted_step,
                parent=token,
            )
            for child in token
        )

    return walk(ast, local_focus=False)


def _context_root(item: Any) -> Any | None:
    """The root node required for absolute paths in an XPath assertion."""
    if isinstance(item, DocumentNode):
        return item
    if isinstance(item, ElementNode):
        return item.get_document_node()
    if isinstance(item, XPathNode):
        parent = getattr(item, "parent", None)
        if isinstance(parent, DocumentNode):
            return parent
        if isinstance(parent, ElementNode):
            return parent.get_document_node()
    return None


def _context_item(items: tuple[Any, ...], parser: XPath31Parser) -> Any:
    """The catalog's initial context item for an XPath assertion.

    The upstream runner supplies an empty document when ``$result`` is empty,
    the single item when there is exactly one, and the empty sequence when
    there are several.  elementpath cannot represent an absent context item,
    so a multi-item result is an explicit adapter gap rather than a substituted
    document.
    """
    if not items:
        return build_node_tree(ET.ElementTree())
    if len(items) == 1:
        return _xpath_value(items[0], parser)
    raise AssertionGap("an XPath assertion context is absent for a multi-item result")


def _parser(declarations: Sequence[tuple[str, str]]) -> XPath31Parser:
    return XPath31Parser(namespaces=_namespace_map(declarations))


def _is_gap(exc: BaseException) -> bool:
    if isinstance(exc, UnsupportedFeatureError):
        return True
    if isinstance(exc, ElementPathSyntaxError):
        return True
    code = getattr(exc, "code", "") or ""
    return code.rsplit(":", 1)[-1] in _GAP_CODES


def _binding(
    declarations: Sequence[tuple[str, str]], items: tuple[Any, ...]
) -> tuple[XPath31Parser, dict[str, Any]]:
    parser = _parser(declarations)
    variables: dict[str, Any] = {"result": [_xpath_value(item, parser) for item in items]}
    return parser, variables


def _translate_evaluation_error(exc: Exception, description: str) -> NoReturn:
    if _is_gap(exc):
        raise AssertionGap(f"cannot evaluate {description}: {exc}") from exc
    raise AssertionFailure(f"{description} failed: {exc}") from exc


def _evaluate(
    expression: str,
    *,
    declarations: Sequence[tuple[str, str]],
    items: tuple[Any, ...] = (),
) -> Any:
    """Compile and evaluate one XPath expression with ``$result`` bound."""
    try:
        parser, variables = _binding(declarations, items)
        ast = parser.parse(expression)
        context_item = _context_item(items, parser)
        root = _context_root(context_item)
        if root is None and _context_node_path(ast):
            raise AssertionGap(
                "an XPath path step requires a node context item: "
                "elementpath would return an empty sequence instead of XPTY0020"
            )
        if root is None:
            return ast.evaluate(XPathContext(item=context_item, variables=variables))
        return ast.evaluate(XPathContext(root=root, item=context_item, variables=variables))
    except AssertionGap:
        raise
    except Exception as exc:  # elementpath's hierarchy is broad
        _translate_evaluation_error(exc, f"assertion XPath {expression!r}")


def _evaluate_context_free(
    expression: str,
    *,
    declarations: Sequence[tuple[str, str]],
    items: tuple[Any, ...] = (),
) -> Any:
    """Evaluate a constructed expression that cannot refer to the context.

    ``assert-type`` is the only caller: its expression is
    ``$result instance of <SequenceType>``, a variable reference followed by a
    sequence type.  Sequence types contain no expressions, so the placeholder
    context item cannot affect the verdict.
    """
    try:
        parser, variables = _binding(declarations, items)
        ast = parser.parse(expression)
        return ast.evaluate(XPathContext(item="", variables=variables))
    except Exception as exc:  # elementpath's hierarchy is broad
        _translate_evaluation_error(exc, f"type assertion {expression!r}")


def _as_expression_items(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (XPathMap, XPathArray)):
        return (value,)
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _evaluate_expression(
    expression: str, declarations: Sequence[tuple[str, str]]
) -> tuple[Any, ...]:
    """Evaluate a standalone expression (``assert-eq``/``assert-deep-eq``)."""
    parser = _parser(declarations)
    try:
        ast = parser.parse(expression)
        if _context_node_path(ast):
            raise AssertionGap(
                f"expected expression {expression!r} uses a context-item path "
                "but no node context is available"
            )
        return _as_expression_items(ast.evaluate(XPathContext(item="")))
    except AssertionGap:
        raise
    except Exception as exc:
        if _is_gap(exc):
            raise AssertionGap(
                f"cannot evaluate expected expression {expression!r}: {exc}"
            ) from exc
        raise AssertionDispute(f"expected expression {expression!r} failed: {exc}") from exc


def _string_value(items: tuple[Any, ...], declarations: Sequence[tuple[str, str]]) -> str:
    """The catalog's string value of the result sequence.

    ``string-join(for $r in $result return string($r), ' ')`` reads only the
    bound ``$result`` variable, never the XPath context item.  It is therefore
    evaluated context-free and works exactly for zero, one, and multiple items.
    """
    expression = "string-join(for $r in $result return string($r), ' ')"
    try:
        parser, variables = _binding(declarations, items)
        ast = parser.parse(expression)
        value = ast.evaluate(XPathContext(item="", variables=variables))
    except Exception as exc:
        _translate_evaluation_error(exc, "result string value")
    return value if isinstance(value, str) else str(value)


def _normalize_space(value: str) -> str:
    return " ".join(value.split())


# ---------------------------------------------------------------------------
# Expected-file and XML-canonicalization helpers


def _resolve_expected_file(case: TestCase | None, file: str) -> Path:
    if case is None or case.document_root is None:
        raise AssertionGap(f"cannot resolve expected file {file!r} without a corpus root")
    reference = file.split("#", 1)[0].split("?", 1)[0]
    relative = Path(reference)
    if relative.is_absolute():
        raise AssertionDispute(f"expected file {file!r} must be relative")
    base = case.result_base if case.result_base is not None else case.source.parent
    path = (case.document_root / base / relative).resolve()
    root = case.document_root.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AssertionDispute(f"expected file {file!r} escapes the corpus root") from exc
    if not path.is_file():
        raise AssertionDispute(f"expected file {file!r} does not exist")
    return path


def _expected_text(
    case: TestCase | None, inline: str, file: str | None, encoding: str | None = None
) -> str:
    if file is None:
        return inline
    data = _resolve_expected_file(case, file).read_bytes()
    if encoding is None:
        declaration = re.search(rb"encoding\s*=\s*[\"']([^\"']+)", data[:256])
        if declaration is not None:
            encoding = declaration.group(1).decode("ascii", "ignore")
    try:
        return data.decode(encoding or "utf-8-sig")
    except (LookupError, UnicodeDecodeError) as exc:
        raise AssertionDispute(f"cannot decode expected file {file!r}: {exc}") from exc


def _strip_xml_declaration(text: str) -> str:
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("<?xml"):
        end = stripped.find("?>")
        if end != -1:
            return stripped[end + 2 :]
    return stripped


@dataclass(frozen=True)
class _CanonicalNode:
    kind: str
    namespace: str = ""
    local: str = ""
    prefix: str = ""
    value: str = ""
    attributes: tuple[tuple[str, str, str], ...] = ()
    children: tuple[_CanonicalNode, ...] = ()


def _split_expat_name(name: str) -> tuple[str, str, str]:
    parts = name.split("\x01")
    if len(parts) == 1:
        return "", parts[0], ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[2]


def _split_attribute_name(name: str) -> tuple[str, str]:
    parts = name.split("\x01")
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[1]


def _canonicalize(text: str) -> _CanonicalNode:
    document = _strip_xml_declaration(text)
    payload = f"<{_WRAPPER}>{document}</{_WRAPPER}>"
    parser = expat.ParserCreate(namespace_separator="\x01")
    parser.namespace_prefixes = True
    parser.buffer_text = True
    stack: list[list[Any]] = []
    roots: list[_CanonicalNode] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        namespace, local, prefix = _split_expat_name(name)
        attrs = []
        for attr_name, attr_value in attributes.items():
            attr_namespace, attr_local = _split_attribute_name(attr_name)
            attrs.append((attr_namespace, attr_local, attr_value))
        stack.append([namespace, local, prefix, tuple(sorted(attrs)), []])

    def end(_: str) -> None:
        namespace, local, prefix, attrs, children = stack.pop()
        node = _CanonicalNode(
            "element",
            namespace=namespace,
            local=local,
            prefix=prefix,
            attributes=attrs,
            children=tuple(children),
        )
        if stack:
            stack[-1][4].append(node)
        else:
            roots.append(node)

    def characters(data: str) -> None:
        stack[-1][4].append(_CanonicalNode("text", value=data))

    def comment(data: str) -> None:
        stack[-1][4].append(_CanonicalNode("comment", value=data))

    def processing_instruction(target: str, data: str) -> None:
        stack[-1][4].append(_CanonicalNode("pi", local=target, value=data))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.CommentHandler = comment
    parser.ProcessingInstructionHandler = processing_instruction
    try:
        parser.Parse(payload, True)
    except expat.ExpatError as exc:
        raise AssertionDispute(f"cannot parse XML result expectation: {exc}") from exc
    return _CanonicalNode("document", children=tuple(roots))


def _unwrap(node: _CanonicalNode) -> tuple[_CanonicalNode, ...]:
    children = node.children[0].children
    if any(child.kind == "element" for child in children):
        return tuple(
            child for child in children if not (child.kind == "text" and not child.value.strip())
        )
    return children


def _canonical_equal(left: _CanonicalNode, right: _CanonicalNode, ignore_prefixes: bool) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "element":
        if (left.namespace, left.local) != (right.namespace, right.local):
            return False
        if not ignore_prefixes and left.prefix != right.prefix:
            return False
        if left.attributes != right.attributes:
            return False
        if len(left.children) != len(right.children):
            return False
        return all(
            _canonical_equal(a, b, ignore_prefixes)
            for a, b in zip(left.children, right.children, strict=True)
        )
    if left.kind in ("text", "comment"):
        return left.value == right.value
    return (left.local, left.value) == (right.local, right.value)


def _xml_equal(expected: str, actual: str, ignore_prefixes: bool) -> bool:
    expected_node = _canonicalize(expected)
    try:
        actual_node = _canonicalize(actual)
    except AssertionDispute as exc:
        raise AssertionFailure(f"actual XML result is not well formed: {exc}") from exc
    expected_children = _unwrap(expected_node)
    actual_children = _unwrap(actual_node)
    if len(expected_children) != len(actual_children):
        return False
    return all(
        _canonical_equal(a, b, ignore_prefixes)
        for a, b in zip(expected_children, actual_children, strict=True)
    )


# ---------------------------------------------------------------------------
# Expected-error normalization


def _normalize_expected_code(
    code: str, declarations: Sequence[tuple[str, str]]
) -> tuple[str | None, str] | None:
    value = code.strip() or "*"
    if value == "*":
        return None
    if value.startswith("Q{"):
        uri, local = value[2:].split("}", 1)
        return (uri or None, local)
    if ":" in value:
        prefix, local = value.split(":", 1)
        namespaces = _namespace_map(declarations)
        resolved = namespaces.get(prefix)
        if resolved is None:
            raise AssertionDispute(f"unresolved error-code prefix {prefix!r} in {code!r}")
        return (resolved, local)
    return (XQT_ERRORS_NS, value)


def _normalize_actual_code(code: str) -> tuple[str | None, str]:
    value = code.strip()
    if value.startswith("Q{"):
        uri, local = value[2:].split("}", 1)
        return (uri or None, local)
    if ":" in value:
        prefix, local = value.split(":", 1)
        if prefix == "err":
            return (XQT_ERRORS_NS, local)
        return (None, value)
    return (XQT_ERRORS_NS, value)


def _display_code(code: tuple[str | None, str] | None) -> str:
    if code is None:
        return "*"
    uri, local = code
    if uri == XQT_ERRORS_NS:
        return local
    if uri is None:
        return f"Q{{}}{local}"
    return f"Q{{{uri}}}{local}"


def _actual_error_code(result: ResultSnapshot) -> str | None:
    return result.error_code if result.error_code is not None else result.serialization_error_code


def _compare_error(
    expected: str, result: ResultSnapshot, declarations: Sequence[tuple[str, str]]
) -> Verdict:
    expected_code = _normalize_expected_code(expected, declarations)
    expected_display = _display_code(expected_code)
    actual_raw = _actual_error_code(result)
    assert actual_raw is not None
    actual_code = _normalize_actual_code(actual_raw)
    actual_display = _display_code(actual_code)
    if expected_code is None:
        return Verdict("pass", expected="*", actual=actual_display)
    if expected_code == actual_code:
        return Verdict("pass", expected=expected_display, actual=actual_display)
    return _wrong(expected_display, actual_display)


# ---------------------------------------------------------------------------
# Assertion implementations


@dataclass(frozen=True)
class AssertionXPath(Assertion):
    expression: str
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = XPATH_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        value = _evaluate(
            f"boolean(({self.expression}))",
            declarations=self.namespaces,
            items=_raw_items(context.result),
        )
        context.record(XPATH_FAMILY)
        return (
            _passed() if bool(value) else _failed(f"assert expression {self.expression!r} is false")
        )


@dataclass(frozen=True)
class AssertionXML(Assertion):
    expected: str = ""
    file: str | None = None
    ignore_prefixes: bool = False
    xml_version: str | None = None
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = XML_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        if self.xml_version == "1.1":
            raise AssertionGap("XML 1.1 expected results are not supported")
        actual = context.result.xml
        if actual is None:
            raise AssertionGap("assert-xml needs the principal XML result")
        context.record(XML_FAMILY)
        expected = _expected_text(context.case, self.expected, self.file)
        return (
            _passed()
            if _xml_equal(expected, actual, self.ignore_prefixes)
            else _failed("actual XML differs from the expected XML")
        )


@dataclass(frozen=True)
class AssertionStringValue(Assertion):
    expected: str = ""
    normalize_space: bool = True
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = STRING_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        context.record(STRING_FAMILY)
        actual = _string_value(_raw_items(context.result), self.namespaces)
        expected = self.expected
        if self.normalize_space:
            actual = _normalize_space(actual)
            expected = _normalize_space(expected)
        if actual == expected:
            return _passed()
        return _failed("result string value differs")


@dataclass(frozen=True)
class AssertionCount(Assertion):
    count: int
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = COUNT_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        context.record(COUNT_FAMILY)
        items = _raw_items(context.result)
        if len(items) == self.count:
            return _passed()
        return _failed(f"expected {self.count} items but found {len(items)}")


@dataclass(frozen=True)
class AssertionType(Assertion):
    type_name: str
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = TYPE_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        matched = _evaluate_context_free(
            f"$result instance of {self.type_name}",
            declarations=self.namespaces,
            items=_raw_items(context.result),
        )
        context.record(TYPE_FAMILY)
        return (
            _passed()
            if bool(matched)
            else _failed(f"result is not an instance of {self.type_name}")
        )


@dataclass(frozen=True)
class AssertionEmpty(Assertion):
    expression: str | None = None
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = EMPTY_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        if self.expression is None:
            context.record(EMPTY_FAMILY)
            return _passed() if not _raw_items(context.result) else _failed("result is not empty")
        value = _evaluate(
            f"empty(({self.expression}))",
            declarations=self.namespaces,
            items=_raw_items(context.result),
        )
        context.record(EMPTY_FAMILY)
        return _passed() if bool(value) else _failed(f"expression {self.expression!r} is not empty")


@dataclass(frozen=True)
class AssertionTrue(Assertion):
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = BOOLEAN_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        context.record(BOOLEAN_FAMILY)
        items = _raw_items(context.result)
        if len(items) == 1 and items[0] is True:
            return _passed()
        return _failed("result is not the singleton boolean true()")


@dataclass(frozen=True)
class AssertionFalse(Assertion):
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = BOOLEAN_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        context.record(BOOLEAN_FAMILY)
        items = _raw_items(context.result)
        if len(items) == 1 and items[0] is False:
            return _passed()
        return _failed("result is not the singleton boolean false()")


def _deep_equal(actual: Sequence[Any], expected: Sequence[Any]) -> bool:
    from elementpath.compare import deep_equal

    try:
        return bool(deep_equal(actual, expected))
    except Exception as exc:
        raise AssertionFailure(f"values are not deep-equal comparable: {exc}") from exc


@dataclass(frozen=True)
class AssertionEq(Assertion):
    expected: str
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = VALUE_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        expected_items = _evaluate_expression(f"({self.expected})", self.namespaces)
        actual_items = _raw_items(context.result)
        parser = _parser(self.namespaces)
        actual_values = [_xpath_value(item, parser) for item in actual_items]
        expected_values = list(expected_items)
        context.record(VALUE_FAMILY)
        if len(actual_values) != 1 or len(expected_values) != 1:
            return _failed("assert-eq requires singleton sequences")
        return (
            _passed()
            if _deep_equal([actual_values[0]], [expected_values[0]])
            else _failed("assert-eq values differ")
        )


@dataclass(frozen=True)
class AssertionDeepEq(Assertion):
    expected: str
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = VALUE_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        expected_items = _evaluate_expression(f"({self.expected})", self.namespaces)
        actual_items = _raw_items(context.result)
        parser = _parser(self.namespaces)
        actual_values = [_xpath_value(item, parser) for item in actual_items]
        context.record(VALUE_FAMILY)
        return (
            _passed()
            if _deep_equal(actual_values, list(expected_items))
            else _failed("assert-deep-eq sequences differ")
        )


@dataclass(frozen=True)
class AssertionPermutation(Assertion):
    expected: str
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = VALUE_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        expected_items = list(_evaluate_expression(f"({self.expected})", self.namespaces))
        actual_items = list(_raw_items(context.result))
        parser = _parser(self.namespaces)
        actual_values = [_xpath_value(item, parser) for item in actual_items]
        context.record(VALUE_FAMILY)
        if len(actual_values) != len(expected_items):
            return _failed("assert-permutation sequences have different lengths")
        remaining = list(expected_items)
        for actual in actual_values:
            for index, expected in enumerate(remaining):
                if _deep_equal([actual], [expected]):
                    remaining.pop(index)
                    break
            else:
                return _failed("assert-permutation sequences differ")
        return _passed() if not remaining else _failed("assert-permutation sequences differ")


@dataclass(frozen=True)
class AssertionMessage(Assertion):
    assertion: Assertion
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = MESSAGE_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        messages = context.result.messages
        if messages is None:
            raise AssertionGap("the result does not provide messages")
        context.record(MESSAGE_FAMILY)
        pending_gap: AssertionGap | None = None
        for message in messages:
            try:
                verdict = self.assertion.evaluate_verdict(context.for_result(message))
            except AssertionGap as exc:
                pending_gap = exc
                continue
            if verdict.passed:
                return verdict
        if pending_gap is not None:
            raise pending_gap
        return _failed("no message satisfies the expected assertion")


@dataclass(frozen=True)
class AssertionResultDocument(Assertion):
    uri: str
    assertion: Assertion
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = SECONDARY_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        secondary = context.result.secondary
        if secondary is None:
            raise AssertionGap("the result does not provide secondary results")
        context.record(SECONDARY_FAMILY)
        candidates = [self.uri]
        if context.case is not None:
            candidates.append(f"{context.case.local_name}/{self.uri}")
            candidates.append(f"{context.case.name}/{self.uri}")
        for candidate in candidates:
            if candidate in secondary:
                return self.assertion.evaluate_verdict(context.for_result(secondary[candidate]))
        suffix = "/" + self.uri.lstrip("/")
        matches = sorted(key for key in secondary if key.endswith(suffix))
        if len(matches) > 1:
            raise AssertionGap(f"secondary result {self.uri!r} is ambiguous")
        if matches:
            return self.assertion.evaluate_verdict(context.for_result(secondary[matches[0]]))
        return _failed(f"secondary result {self.uri!r} was not produced")


@dataclass(frozen=True)
class AssertionSerialization(Assertion):
    expected: str = ""
    file: str | None = None
    method: str | None = None
    encoding: str | None = None
    bom: bool | None = None
    ignore_prefixes: bool = False
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = SERIALIZATION_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        if self.ignore_prefixes:
            raise AssertionGap("prefix-insensitive serialization comparison is not implemented")
        actual = context.result.serialized
        if actual is None:
            raise AssertionGap("assert-serialization needs the engine serialization")
        context.record(SERIALIZATION_FAMILY)
        if self.bom is not None:
            if context.result.serialization_bom is None:
                raise AssertionGap("serialization BOM state is unavailable")
            if context.result.serialization_bom != self.bom:
                return _failed("serialization BOM differs")
        if self.method is not None:
            actual_method = context.result.serialization_method
            if actual_method is None:
                raise AssertionGap("the serialization method is unavailable")
            if self.method != actual_method:
                raise AssertionGap(
                    f"the result was serialized with {actual_method!r}, not {self.method!r}"
                )
        if self.encoding is not None:
            actual_encoding = context.result.serialization_encoding
            if actual_encoding is None:
                raise AssertionGap("the serialization encoding is unavailable")
            if self.encoding != actual_encoding:
                return _failed("serialization encoding differs")
        expected = _expected_text(context.case, self.expected, self.file, self.encoding)
        if actual.replace("\r\n", "\n").replace("\r", "\n") == expected.replace(
            "\r\n", "\n"
        ).replace("\r", "\n"):
            return _passed()
        return _failed("serialization differs from the expected text")


@dataclass(frozen=True)
class AssertionSerializationMatches(Assertion):
    pattern: str = ""
    file: str | None = None
    flags: str = ""
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = SERIALIZATION_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        actual = context.result.serialized
        if actual is None:
            raise AssertionGap("serialization-matches needs the engine serialization")
        context.record(SERIALIZATION_FAMILY)
        pattern = _expected_text(context.case, self.pattern, self.file)
        parser = _parser(self.namespaces)
        try:
            ast = parser.parse("matches($value, $pattern, $flags)")
            matched = ast.evaluate(
                XPathContext(
                    item="",
                    variables={"value": actual, "pattern": pattern, "flags": self.flags},
                )
            )
        except Exception as exc:
            raise AssertionDispute(f"invalid serialization pattern {pattern!r}: {exc}") from exc
        return _passed() if bool(matched) else _failed("serialization does not match the pattern")


@dataclass(frozen=True)
class AssertionSerializationError(Assertion):
    code: str = "*"
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = EXPECTED_ERROR_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        context.record(EXPECTED_ERROR_FAMILY)
        if not context.error_mode:
            expected = _display_code(_normalize_expected_code(self.code, self.namespaces))
            return Verdict(
                "fail", expected=expected, detail=f"expected serialization error {expected}"
            )
        return _compare_error(self.code, context.result, self.namespaces)


@dataclass(frozen=True)
class AssertionExpectedError(Assertion):
    code: str = "*"
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = EXPECTED_ERROR_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        context.record(EXPECTED_ERROR_FAMILY)
        if not context.error_mode:
            expected = _display_code(_normalize_expected_code(self.code, self.namespaces))
            return Verdict(
                "fail", expected=expected, detail=f"expected error {expected} but none was raised"
            )
        return _compare_error(self.code, context.result, self.namespaces)


@dataclass(frozen=True)
class AssertionWarning(Assertion):
    namespaces: tuple[tuple[str, str], ...] = ()
    family: ClassVar[str] = WARNING_FAMILY

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            return _failed("the transformation raised an unexpected error")
        warnings = context.result.warnings
        if warnings is None:
            raise AssertionGap("the result does not provide warnings")
        context.record(WARNING_FAMILY)
        return _passed() if warnings else _failed("no warning was emitted")


@dataclass(frozen=True)
class UnsupportedAssertion(Assertion):
    name: str
    namespaces: tuple[tuple[str, str], ...] = ()

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        raise AssertionGap(f"unsupported assertion {self.name!r}")


@dataclass(frozen=True)
class AllOf(Assertion):
    assertions: tuple[Assertion, ...]
    namespaces: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assertions", tuple(self.assertions))

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        first_wrong: Verdict | None = None
        first_gap: AssertionGap | None = None
        for assertion in self.assertions:
            try:
                verdict = assertion.evaluate_verdict(context)
            except AssertionGap as exc:
                first_gap = first_gap or exc
                continue
            if verdict.status == "fail":
                return verdict
            if verdict.status == "wrong-error":
                first_wrong = first_wrong or verdict
        if first_wrong is not None:
            return first_wrong
        if first_gap is not None:
            raise first_gap
        return _passed()


@dataclass(frozen=True)
class AnyOf(Assertion):
    assertions: tuple[Assertion, ...]
    namespaces: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assertions", tuple(self.assertions))

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        first_wrong: Verdict | None = None
        first_gap: AssertionGap | None = None
        for assertion in self.assertions:
            try:
                verdict = assertion.evaluate_verdict(context)
            except AssertionGap as exc:
                first_gap = first_gap or exc
                continue
            if verdict.passed:
                return verdict
            if verdict.status == "wrong-error":
                first_wrong = first_wrong or verdict
        if first_wrong is not None:
            return first_wrong
        if first_gap is not None:
            raise first_gap
        return _failed("no alternative assertion passed")


@dataclass(frozen=True)
class Not(Assertion):
    assertion: Assertion
    namespaces: tuple[tuple[str, str], ...] = ()

    def evaluate_verdict(self, context: AssertionContext) -> Verdict:
        if context.error_mode:
            # Mirrors the catalog runner's ``expects-error`` mode: a ``not``
            # wrapper cannot accept any error.
            return _failed("a not() assertion cannot accept an error result")
        verdict = self.assertion.evaluate_verdict(context)
        if verdict.passed:
            return _failed("the negated assertion passed")
        return _passed()


# ---------------------------------------------------------------------------
# Parsing


def _xml_namespace_scopes(xml: str) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Recover in-scope namespaces from a serialized assertion document."""
    scopes: list[tuple[tuple[str, str], ...]] = []
    stack: list[dict[str, str]] = []
    pending: list[tuple[str, str]] = []
    try:
        for event, element in ET.iterparse(io.StringIO(xml), events=("start", "start-ns", "end")):
            if event == "start-ns":
                pending.append((element[0] or "", element[1]))
            elif event == "start":
                scope = dict(stack[-1]) if stack else {}
                scope.update(pending)
                pending = []
                stack.append(scope)
                scopes.append(tuple(scope.items()))
            else:
                stack.pop()
    except ET.ParseError:
        return ()
    return tuple(scopes)


def _single_child(element: ET.Element) -> ET.Element:
    children = [child for child in element if isinstance(child.tag, str)]
    if len(children) != 1:
        raise AssertionDispute(f"<{_local_name(element.tag)}> requires exactly one assertion child")
    return children[0]


def _assertion_text(element: ET.Element) -> str:
    return element.text or ""


def _build_assertion(element: ET.Element, scopes: Any) -> Assertion:
    scope = next(scopes, ())
    name = _local_name(element.tag)
    text = _assertion_text(element)
    if name == "assert":
        return AssertionXPath(text, namespaces=scope)
    if name == "assert-xml":
        return AssertionXML(
            text,
            file=element.get("file"),
            ignore_prefixes=_optional_bool(element.get("ignore-prefixes")),
            xml_version=element.get("xml-version"),
            namespaces=scope,
        )
    if name == "assert-string-value":
        return AssertionStringValue(
            text,
            normalize_space=_optional_bool(element.get("normalize-space"), True),
            namespaces=scope,
        )
    if name == "assert-count":
        value = text.strip()
        try:
            count = int(value)
        except ValueError as exc:
            raise AssertionDispute(f"assert-count value {value!r} is not an integer") from exc
        return AssertionCount(count, namespaces=scope)
    if name == "assert-type":
        return AssertionType(text.strip(), namespaces=scope)
    if name == "assert-empty":
        return AssertionEmpty(text.strip() or None, namespaces=scope)
    if name == "assert-true":
        return AssertionTrue(namespaces=scope)
    if name == "assert-false":
        return AssertionFalse(namespaces=scope)
    if name == "assert-eq":
        return AssertionEq(text, namespaces=scope)
    if name == "assert-deep-eq":
        return AssertionDeepEq(text, namespaces=scope)
    if name == "assert-permutation":
        return AssertionPermutation(text, namespaces=scope)
    if name == "assert-message":
        return AssertionMessage(_build_assertion(_single_child(element), scopes), namespaces=scope)
    if name == "assert-result-document":
        uri = element.get("uri")
        if uri is None or not uri.strip():
            raise AssertionDispute("assert-result-document requires a uri attribute")
        return AssertionResultDocument(
            uri.strip(), _build_assertion(_single_child(element), scopes), namespaces=scope
        )
    if name == "assert-serialization":
        return AssertionSerialization(
            text,
            file=element.get("file"),
            method=element.get("method"),
            encoding=element.get("encoding"),
            bom=_optional_bool(element.get("bom")) if element.get("bom") is not None else None,
            ignore_prefixes=_optional_bool(element.get("ignore-prefixes")),
            namespaces=scope,
        )
    if name == "serialization-matches":
        return AssertionSerializationMatches(
            text,
            file=element.get("file"),
            flags=element.get("flags") or "",
            namespaces=scope,
        )
    if name == "assert-serialization-error":
        return AssertionSerializationError(element.get("code") or "*", namespaces=scope)
    if name == "assert-warning":
        return AssertionWarning(namespaces=scope)
    if name == "error":
        return AssertionExpectedError(element.get("code") or "*", namespaces=scope)
    if name == "all-of":
        return AllOf(
            tuple(
                _build_assertion(child, scopes) for child in element if isinstance(child.tag, str)
            ),
            namespaces=scope,
        )
    if name == "any-of":
        return AnyOf(
            tuple(
                _build_assertion(child, scopes) for child in element if isinstance(child.tag, str)
            ),
            namespaces=scope,
        )
    if name == "not":
        return Not(_build_assertion(_single_child(element), scopes), namespaces=scope)
    return UnsupportedAssertion(name, namespaces=scope)


def parse_result(
    result_xml: str,
    namespace_scopes: tuple[tuple[tuple[str, str], ...], ...] = (),
) -> Assertion:
    """Parse a catalog ``<result>`` fragment into a nested assertion tree."""
    if not result_xml.strip():
        raise AssertionDispute("test case has no expected result")
    try:
        root = ET.fromstring(result_xml)
    except ET.ParseError as exc:
        raise AssertionDispute(f"expected result is not well formed: {exc}") from exc
    if not _in_catalog_namespace(root.tag) or _local_name(root.tag) != "result":
        raise AssertionDispute(f"expected a <result> document, found <{_local_name(root.tag)}>")
    children = [child for child in root if isinstance(child.tag, str)]
    if len(children) != 1:
        raise AssertionDispute(
            f"a <result> must contain exactly one assertion, found {len(children)}"
        )
    scopes = list(namespace_scopes) if namespace_scopes else list(_xml_namespace_scopes(result_xml))
    element_count = len(list(root.iter()))
    if len(scopes) < element_count:
        scopes.extend([()] * (element_count - len(scopes)))
    iterator = iter(scopes)
    next(iterator, ())  # the <result> element itself
    return _build_assertion(children[0], iterator)


# ---------------------------------------------------------------------------
# Public entry points


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def evaluate_assertion(
    assertion: Assertion,
    *,
    raw_result: Any = _MISSING,
    result: ResultSnapshot | None = None,
    case: TestCase | None = None,
) -> bool:
    """Evaluate one assertion against a raw value or a result snapshot.

    A raw value is one item unless it is a tuple/list, which is treated as a
    sequence.  ``AssertionGap`` and ``AssertionDispute`` propagate so callers
    can report the honest harness outcome; an unusable actual result is a
    false assertion.
    """
    if result is None:
        value = () if raw_result is _MISSING else _as_sequence(raw_result)
        result = ResultSnapshot(raw=value)
    context = AssertionContext(result, case=case, error_mode=_actual_error_code(result) is not None)
    try:
        return assertion.evaluate_verdict(context).passed
    except AssertionFailure:
        return False


def assert_case(
    case: TestCase,
    result: ResultSnapshot | None = None,
    *,
    raw_result: Any = _MISSING,
) -> CaseResult:
    """Decide one catalog case from its actual result."""
    if result is None:
        value = () if raw_result is _MISSING else _as_sequence(raw_result)
        result = ResultSnapshot(raw=value)
    if not case.result_xml.strip():
        return CaseResult.corpus_dispute(case.name, detail="test case has no expected result")
    try:
        assertion = parse_result(case.result_xml, case.namespace_scopes)
    except AssertionDispute as exc:
        return CaseResult.corpus_dispute(case.name, detail=str(exc))
    actual_code = _actual_error_code(result)
    context = AssertionContext(result, case=case, error_mode=actual_code is not None)
    try:
        verdict = assertion.evaluate_verdict(context)
    except AssertionGap as exc:
        return CaseResult.adapter_gap(case.name, detail=str(exc), coverage=context.coverage)
    except AssertionDispute as exc:
        return CaseResult.corpus_dispute(case.name, detail=str(exc), coverage=context.coverage)
    except AssertionFailure as exc:
        return CaseResult.failed(case.name, message=str(exc), coverage=context.coverage)
    if verdict.status == "wrong-error":
        return CaseResult.expected_error(
            verdict.expected or "*",
            verdict.actual,
            name=case.name,
            coverage=context.coverage,
        )
    if verdict.passed:
        if context.error_mode:
            return CaseResult.expected_error(
                verdict.expected or "*",
                verdict.actual or actual_code,
                name=case.name,
                coverage=context.coverage,
            )
        return CaseResult.passed(case.name, coverage=context.coverage)
    if verdict.expected is not None:
        return CaseResult.expected_error(
            verdict.expected,
            verdict.actual,
            name=case.name,
            coverage=context.coverage,
        )
    return CaseResult.failed(case.name, message=verdict.detail, coverage=context.coverage)
