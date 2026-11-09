"""Catalog-driven W3C QT3 diagnostics for the installed XPath substrate.

The harness selects XPath test cases from a pinned ``w3c/qt3tests`` checkout,
evaluates their expressions with elementpath, and reports one explicit status
per case.  It is deliberately separate from the XSLT catalog, baselines, and
pass counts: these outcomes are substrate evidence, never XSLT conformance.

Selection follows the upstream QT3 driver's host-language rule: the last
``spec`` dependency seen (test-set dependencies first, then test-case
dependencies) chooses XPath or XQuery.  A case that selects XQuery is
``untested`` here.  XPath cases whose environment needs sources, schemas,
resources, collections, parameters, or a context item are adapter
requirements because this diagnostic does not build those facilities.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import elementpath
from elementpath import XPathContext
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath3 import XPath30Parser, XPath31Parser

#: Namespace of the QT3 catalog and test-set documents.
CATALOG_NS = "http://www.w3.org/2010/09/qt-fots-catalog"

#: The pinned corpus submodule, relative to the repository root.
CORPUS_PATH = "tests/xpath/corpus"

#: Repository root, so a default corpus path works from any directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the corpus can be checked out.
CORPUS_COMMAND = f"git submodule update --init {CORPUS_PATH}"

#: The test sets that exercise this task's values, contexts, and function items.
QT3_TEST_SETS = (
    "prod-MapConstructor",
    "prod-SquareArrayConstructor",
    "prod-CurlyArrayConstructor",
    "prod-NamedFunctionRef",
    "prod-InlineFunctionExpr",
    "prod-Predicate",
    "fn-apply",
    "fn-function-lookup",
    "fn-function-arity",
    "fn-function-name",
    "map-get",
    "map-size",
    "misc-HigherOrderFunctions",
)

#: QT3 features this diagnostic can exercise without extra facilities.
SUPPORTED_FEATURES = frozenset({"higherOrderFunctions"})

#: Statuses a QT3 diagnostic result may use.
QT3_STATUSES = frozenset({"pass", "defect", "adapter-requirement", "untested"})

#: Report schema version.
REPORT_SCHEMA = 1

DEFAULT_NAMESPACES: Mapping[str, str] = {
    "fn": "http://www.w3.org/2005/xpath-functions",
    "map": "http://www.w3.org/2005/xpath-functions/map",
    "array": "http://www.w3.org/2005/xpath-functions/array",
    "math": "http://www.w3.org/2005/xpath-functions/math",
    "xs": "http://www.w3.org/2001/XMLSchema",
    "err": "http://www.w3.org/2005/xqt-errors",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

_VERSION_ALIASES = {"2": "2.0", "2.0": "2.0", "3": "3.0", "3.0": "3.0", "3.1": "3.1"}
_PARSERS: dict[str, Any] = {
    "2.0": XPath2Parser,
    "3.0": XPath30Parser,
    "3.1": XPath31Parser,
}
_ENVIRONMENT_RESOURCES = frozenset(
    {"source", "schema", "resource", "collection", "param", "context-item"}
)
_UNSUPPORTED_ASSERTIONS = frozenset(
    {
        "assert-xml",
        "assert-permutation",
        "assert-serialization",
        "assert-serialization-error",
        "serialization-matches",
    }
)


class Qt3CorpusError(Exception):
    """The pinned QT3 corpus is absent or structurally unusable."""


@dataclass(frozen=True)
class Qt3Case:
    """One selected QT3 test case, with its environment requirements."""

    name: str
    test_set: str
    expression: str
    result: ET.Element | None
    dependencies: tuple[tuple[str, str | None, str | None], ...]
    namespaces: Mapping[str, str]
    environment_children: tuple[str, ...]


@dataclass(frozen=True)
class Qt3Catalog:
    """The master catalog's test-set index and shared environment table."""

    path: Path
    root: Path
    revision: str
    test_sets: Mapping[str, Path]
    environments: Mapping[str, ET.Element]

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_sets", dict(self.test_sets))
        object.__setattr__(self, "environments", dict(self.environments))


@dataclass(frozen=True)
class Qt3CaseResult:
    """The explicit diagnostic status of one QT3 case."""

    name: str
    test_set: str
    status: str
    message: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a QT3 result requires a case name")
        if self.status not in QT3_STATUSES:
            raise ValueError(f"unknown QT3 status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_set": self.test_set,
            "status": self.status,
            "message": self.message,
        }


@dataclass(frozen=True)
class Qt3Report:
    """One XPath version's diagnostic report over the selected QT3 cases."""

    xpath_version: str
    parser: str
    parser_version: str
    evaluator: str
    evaluator_version: str
    corpus_commit: str
    cases: tuple[Qt3CaseResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))
        names = [case.name for case in self.cases]
        if len(names) != len(set(names)):
            raise ValueError("QT3 reports require unique case names")

    def summary(self) -> dict[str, int]:
        counts = Counter(case.status for case in self.cases)
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "suite": "qt3",
            "xpath_version": self.xpath_version,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
            "corpus_commit": self.corpus_commit,
            "summary": self.summary(),
            "cases": {
                case.name: case.to_dict() for case in sorted(self.cases, key=lambda c: c.name)
            },
        }


def _normalise_version(version: str) -> str:
    normalized = _VERSION_ALIASES.get(version.strip())
    if normalized is None:
        raise ValueError(f"unknown XPath version: {version!r}")
    return normalized


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> tuple[ET.Element, ...]:
    return tuple(child for child in element if _local_name(child.tag) == name)


def _dependencies(element: ET.Element) -> tuple[tuple[str, str | None, str | None], ...]:
    return tuple(
        (child.get("type", ""), child.get("value"), child.get("satisfied"))
        for child in _children(element, "dependency")
    )


def _corpus_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _resolve_reference(root: Path, base: Path, reference: str) -> Path:
    if not reference:
        raise Qt3CorpusError("a QT3 test-set reference requires a file")
    raw = reference.split("#", 1)[0].split("?", 1)[0]
    path = Path(raw)
    if path.is_absolute():
        raise Qt3CorpusError(f"QT3 test-set reference {reference!r} must be relative")
    resolved = (base / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise Qt3CorpusError(f"QT3 test-set reference {reference!r} escapes the corpus") from exc
    return resolved


def load_catalog(path: str | Path | None = None) -> Qt3Catalog:
    """Load the master QT3 catalog and index its test sets and environments.

    *path* may be a catalog document or a corpus directory.  A missing corpus
    reports the exact submodule checkout command instead of an empty run.
    """
    catalog_path = Path(path) if path is not None else REPO_ROOT / CORPUS_PATH
    if catalog_path.is_dir():
        catalog_path = catalog_path / "catalog.xml"
    if not catalog_path.is_file():
        raise Qt3CorpusError(
            f"QT3 catalog {catalog_path} is absent; initialize the corpus with {CORPUS_COMMAND!r}"
        )
    try:
        root = ET.parse(catalog_path).getroot()
    except ET.ParseError as exc:
        raise Qt3CorpusError(f"QT3 catalog {catalog_path} is not well formed: {exc}") from exc
    if _local_name(root.tag) != "catalog":
        raise Qt3CorpusError(f"{catalog_path} is not a QT3 catalog document")

    root_dir = catalog_path.parent
    test_sets: dict[str, Path] = {}
    for declaration in _children(root, "test-set"):
        name = declaration.get("name")
        reference = declaration.get("file")
        if not name or not reference:
            raise Qt3CorpusError(f"invalid QT3 test-set declaration in {catalog_path}")
        if name in test_sets:
            raise Qt3CorpusError(f"duplicate QT3 test-set name: {name}")
        test_sets[name] = _resolve_reference(root_dir, root_dir, reference)

    environments = {
        declaration.get("name", ""): declaration
        for declaration in _children(root, "environment")
        if declaration.get("name")
    }
    return Qt3Catalog(
        path=catalog_path,
        root=root_dir,
        revision=_corpus_revision(root_dir),
        test_sets=test_sets,
        environments=environments,
    )


def _document_namespaces(path: Path) -> dict[str, str]:
    """Collect in-scope namespace prefixes from a parsed test-set document.

    ``ElementTree`` expands qualified names and drops ``xmlns`` declarations
    from the parsed tree, so namespace prefixes must be recovered with the
    ``start-ns`` events.
    """
    namespaces: dict[str, str] = {}
    for _, declaration in ET.iterparse(path, events=("start-ns",)):
        prefix, uri = declaration
        if prefix:
            namespaces[prefix] = uri
    return namespaces


def _test_set_name(document: ET.Element, fallback: str) -> str:
    return document.get("name") or fallback


def _iter_cases(
    catalog: Qt3Catalog,
    test_sets: Sequence[str] = QT3_TEST_SETS,
) -> tuple[Qt3Case, ...]:
    """Read every test case in the declared QT3 test sets."""
    cases: list[Qt3Case] = []
    for name in test_sets:
        path = catalog.test_sets.get(name)
        if path is None:
            raise Qt3CorpusError(f"QT3 catalog does not declare test set {name!r}")
        if not path.is_file():
            raise Qt3CorpusError(
                f"QT3 test set {name!r} is absent at {path}; run {CORPUS_COMMAND!r}"
            )
        document = ET.parse(path).getroot()
        set_name = _test_set_name(document, name)
        document_namespaces = _document_namespaces(path)
        shared = dict(catalog.environments)
        shared.update(
            {
                declaration.get("name", ""): declaration
                for declaration in _children(document, "environment")
                if declaration.get("name")
            }
        )
        set_dependencies = _dependencies(document)
        for case in _children(document, "test-case"):
            case_name = case.get("name") or ""
            expression_node = next(iter(_children(case, "test")), None)
            result = next(iter(_children(case, "result")), None)
            environment = next(iter(_children(case, "environment")), None)
            if environment is not None and environment.get("ref") is not None:
                environment = shared.get(environment.get("ref", ""))
            environment_children = (
                tuple(_local_name(child.tag) for child in environment)
                if environment is not None
                else ()
            )
            if environment is None and _children(case, "environment"):
                environment_children = ("missing",)
            namespaces = dict(DEFAULT_NAMESPACES)
            namespaces.update(document_namespaces)
            if environment is not None:
                for child in _children(environment, "namespace"):
                    prefix = child.get("prefix", "")
                    uri = child.get("uri", "")
                    if prefix and uri:
                        namespaces[prefix] = uri
            cases.append(
                Qt3Case(
                    name=f"{set_name}/{case_name}",
                    test_set=set_name,
                    expression=(expression_node.text or "").strip()
                    if expression_node is not None
                    else "",
                    result=result,
                    dependencies=set_dependencies + _dependencies(case),
                    namespaces=namespaces,
                    environment_children=environment_children,
                )
            )
    return tuple(cases)


def _xpath_spec(
    dependencies: Iterable[tuple[str, str | None, str | None]],
) -> tuple[str, tuple[str, ...]] | None:
    """The last ``spec`` dependency chooses the host language, as upstream does."""
    selected: tuple[str, tuple[str, ...]] | None = None
    for kind, value, satisfied in dependencies:
        if kind != "spec" or satisfied == "false":
            continue
        tokens = tuple((value or "").split())
        xp = tuple(token for token in tokens if token.startswith("XP"))
        xq = tuple(token for token in tokens if token.startswith("XQ"))
        xt = tuple(token for token in tokens if token.startswith("XT"))
        if xp:
            selected = ("XP", xp)
        elif xq:
            selected = ("XQ", xq)
        elif xt:
            selected = ("XT", xt)
    return selected


def _version_matches(tokens: Sequence[str], version: str) -> bool:
    current = tuple(int(part) for part in version.split("."))
    for token in tokens:
        suffix = token[2:]
        forward = suffix.endswith("+")
        digits = suffix[:-1] if forward else suffix
        if len(digits) != 2 or not digits.isdigit():
            continue
        required = (int(digits[0]), int(digits[1]))
        matched = current >= required if forward else current == required
        if matched:
            return True
    return False


def _feature_requirement(
    dependencies: Iterable[tuple[str, str | None, str | None]],
) -> str | None:
    for kind, value, satisfied in dependencies:
        if kind == "feature" and satisfied != "false" and value not in SUPPORTED_FEATURES:
            return value
    return None


class _AssertionContextRequired(Exception):
    """A QT3 assertion expression depends on the absent query context item."""


def _evaluate(parser: Any, expression: str) -> Any:
    """Evaluate a QT3 test expression with an exactly absent context item.

    ``XPathToken.evaluate(None)`` is elementpath's absent-context path: a
    context-independent expression evaluates, while an expression that needs
    the context item raises ``XPDY0002``.  No placeholder context item is
    supplied.
    """
    return parser.parse(expression).evaluate(None)


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", "") or ""
    return code.rsplit(":", 1)[-1]


def _error_label(error: BaseException) -> str:
    return _error_code(error) or type(error).__name__


def _declared_error_code(element: ET.Element) -> str:
    code = element.get("code", "") or ""
    return code.rsplit(":", 1)[-1]


def _first_declared_error(element: ET.Element) -> str | None:
    if _local_name(element.tag) == "error":
        return _declared_error_code(element)
    for child in element:
        code = _first_declared_error(child)
        if code is not None:
            return code
    return None


def _accepts_absent_context_error(element: ET.Element | None) -> bool:
    if element is None:
        return False
    return any(
        _local_name(child.tag) == "error" and _declared_error_code(child) in {"XPDY0002", "*"}
        for child in element.iter()
    )


def _first_unsupported_assertion(element: ET.Element) -> str | None:
    if _local_name(element.tag) in _UNSUPPORTED_ASSERTIONS:
        return _local_name(element.tag)
    for child in element:
        name = _first_unsupported_assertion(child)
        if name is not None:
            return name
    return None


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


_CONTEXT_FUNCTIONS = frozenset(
    {
        "position",
        "last",
        "string",
        "string-length",
        "normalize-space",
        "number",
        "name",
        "local-name",
        "namespace-uri",
        "root",
        "base-uri",
        "document-uri",
        "node-name",
        "lang",
        "nilled",
        "data",
    }
)

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
    return symbol in _STEP_SYMBOLS or symbol == "." or (symbol == "*" and len(token) == 0)


def _uses_context_item(ast: Any) -> bool:
    """Whether an assertion expression statically depends on the query context.

    This is deliberately conservative: a bare ``.``, focus functions, a
    context-rooted path, or a top-level/axis step is context-dependent.  A
    variable-rooted path such as ``$result/a`` is not.
    """

    def walk(
        token: Any,
        *,
        local_focus: bool = False,
        rooted_step: bool = False,
        parent: Any | None = None,
    ) -> bool:
        symbol = getattr(token, "symbol", None)
        type_context = (
            parent is not None and getattr(parent, "symbol", None) in _TYPE_PARENT_SYMBOLS
        )
        if symbol == "." or symbol in {"position", "last"}:
            return True
        if symbol in _CONTEXT_FUNCTIONS and len(token) == 0:
            return True
        if symbol in ("/", "//"):
            if len(token) < 2:
                return True
            left, right = token[0], token[1]
            if _is_step(left):
                return not type_context
            return walk(left, local_focus=local_focus, parent=token) or walk(
                right,
                local_focus=local_focus,
                rooted_step=True,
                parent=token,
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
        if symbol == "$":
            return False
        if symbol == "(name)":
            if type_context or (parent is not None and getattr(parent, "symbol", None) == "$"):
                return False
            return not rooted_step and not local_focus
        if _is_step(token):
            if type_context:
                return False
            return not rooted_step and not local_focus
        return any(
            walk(
                child,
                local_focus=local_focus,
                rooted_step=rooted_step,
                parent=token,
            )
            for child in token
        )

    return walk(ast)


def _assertion_value(
    expression: str,
    namespaces: Mapping[str, str],
    parser_class: Any,
    variables: Mapping[str, Any] | None = None,
) -> Any:
    ast = parser_class(namespaces=namespaces).parse(expression)
    if _uses_context_item(ast):
        raise _AssertionContextRequired(expression)
    return ast.evaluate(XPathContext(item="", variables=dict(variables or {})))


def _assert_element(
    element: ET.Element,
    result: list[Any],
    error: BaseException | None,
    namespaces: Mapping[str, str],
    parser_class: Any,
) -> bool | None:
    kind = _local_name(element.tag)
    text = (element.text or "").strip()
    if kind == "all-of":
        outcomes = [
            _assert_element(child, result, error, namespaces, parser_class) for child in element
        ]
        if any(outcome is False for outcome in outcomes):
            return False
        return None if any(outcome is None for outcome in outcomes) else True
    if kind == "any-of":
        outcomes = [
            _assert_element(child, result, error, namespaces, parser_class) for child in element
        ]
        if any(outcome is True for outcome in outcomes):
            return True
        return None if any(outcome is None for outcome in outcomes) else False
    if kind == "not":
        outcomes = [
            _assert_element(child, result, error, namespaces, parser_class) for child in element
        ]
        if any(outcome is None for outcome in outcomes):
            return None
        return not all(outcomes)
    if kind == "error":
        if error is None:
            return False
        expected = _declared_error_code(element)
        actual = _error_code(error)
        return expected in {"", "*"} or expected == actual
    if kind in _UNSUPPORTED_ASSERTIONS:
        return None
    if error is not None:
        return False
    if kind in {"assert-true", "assert-false", "assert-empty", "assert-count", "assert-type"}:
        if kind == "assert-true":
            return result == [True]
        if kind == "assert-false":
            return result == [False]
        if kind == "assert-empty":
            return not result
        if kind == "assert-count":
            return len(result) == int(text)
        return bool(
            _assertion_value(
                f"$result instance of {text}",
                namespaces,
                parser_class,
                {"result": result},
            )
        )
    if kind in {"assert-eq", "assert-deep-eq"}:
        return bool(
            _assertion_value(
                f"deep-equal($result, ({text}))",
                namespaces,
                parser_class,
                {"result": result},
            )
        )
    if kind == "assert-string-value":
        actual = _assertion_value(
            "string-join(for $r in $result return string($r), ' ')",
            namespaces,
            parser_class,
            {"result": result},
        )
        return actual == text
    if kind == "assert":
        return bool(
            _assertion_value(
                f"boolean({text})",
                namespaces,
                parser_class,
                {"result": result},
            )
        )
    return None


def _evaluate_case(case: Qt3Case, version: str, parser_class: Any) -> Qt3CaseResult:
    spec = _xpath_spec(case.dependencies)
    if spec is None:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "untested",
            "no XPath 2.0/3.0/3.1 spec dependency (XQuery-only or unclassified)",
        )
    language, tokens = spec
    if language != "XP":
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "untested",
            f"host language is {language}, not XPath",
        )
    if not _version_matches(tokens, version):
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "untested",
            f"requires XPath {' '.join(tokens)}",
        )
    feature = _feature_requirement(case.dependencies)
    if feature is not None:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "untested",
            f"requires unsupported feature {feature}",
        )
    if "missing" in case.environment_children:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            "the referenced QT3 environment was not found",
        )
    resources = [child for child in case.environment_children if child in _ENVIRONMENT_RESOURCES]
    if resources:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            f"environment requires {resources[0]}",
        )

    parser = parser_class(namespaces=case.namespaces)
    try:
        first = _evaluate(parser, case.expression)
        first_error = None
    except Exception as exc:
        first = None
        first_error = exc
    if (
        first_error is not None
        and _error_code(first_error) == "XPDY0002"
        and not _accepts_absent_context_error(case.result)
    ):
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            "requires an absent context item that elementpath cannot "
            "evaluate this expression without",
        )

    if case.result is None:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            "test case has no expected result",
        )
    assertions = list(case.result)
    if not assertions:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            "test case has an empty expected result",
        )
    try:
        outcomes = [
            _assert_element(
                assertion,
                _items(first),
                first_error,
                case.namespaces,
                parser_class,
            )
            for assertion in assertions
        ]
    except _AssertionContextRequired:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            "the expected assertion requires a query context item",
        )
    except Exception as exc:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "defect",
            f"the expected assertion could not be evaluated: {type(exc).__name__}: {exc}",
        )
    if any(outcome is None for outcome in outcomes):
        unsupported = _first_unsupported_assertion(case.result)
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "adapter-requirement",
            f"unsupported assertion {unsupported}",
        )
    if all(outcomes):
        return Qt3CaseResult(case.name, case.test_set, "pass", "matched")
    if first_error is not None:
        expected = _first_declared_error(case.result)
        if expected is None:
            return Qt3CaseResult(
                case.name,
                case.test_set,
                "defect",
                f"unexpected error {_error_label(first_error)}",
            )
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "defect",
            f"wrong error: expected {expected}, got {_error_label(first_error)}",
        )
    expected_error = next(
        (
            code
            for assertion in assertions
            if (code := _first_declared_error(assertion)) is not None
        ),
        None,
    )
    if expected_error is not None:
        return Qt3CaseResult(
            case.name,
            case.test_set,
            "defect",
            f"expected error {expected_error} but no error was raised",
        )
    return Qt3CaseResult(
        case.name,
        case.test_set,
        "defect",
        "the expected assertions did not match the evaluated result",
    )


def run_qt3_diagnostics(
    version: str,
    *,
    corpus: str | Path | None = None,
    test_sets: Sequence[str] = QT3_TEST_SETS,
) -> Qt3Report:
    """Run the selected QT3 XPath cases and return one explicit report."""
    normalized = _normalise_version(version)
    if not test_sets:
        raise ValueError("at least one QT3 test set is required")
    parser_class = _PARSERS[normalized]
    catalog = load_catalog(corpus)
    cases = _iter_cases(catalog, test_sets)
    results = tuple(_evaluate_case(case, normalized, parser_class) for case in cases)
    return Qt3Report(
        xpath_version=normalized,
        parser=parser_class.__name__,
        parser_version=getattr(parser_class, "version", ""),
        evaluator="elementpath",
        evaluator_version=elementpath.__version__,
        corpus_commit=catalog.revision,
        cases=results,
    )


def render_json(reports: Sequence[Qt3Report]) -> str:
    """Render deterministic JSON for one or more version reports."""
    document = {
        "schema": REPORT_SCHEMA,
        "suite": "qt3-diagnostics",
        "reports": [report.to_dict() for report in reports],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_text(reports: Sequence[Qt3Report]) -> str:
    """Render summaries and every non-passing case for human review."""
    lines: list[str] = []
    for report in reports:
        summary = ", ".join(f"{status}={count}" for status, count in report.summary().items())
        lines.append(
            f"QT3 XPath {report.xpath_version} ({report.parser}, "
            f"elementpath {report.evaluator_version}, corpus {report.corpus_commit[:12] or 'unknown'})"
        )
        lines.append(summary)
        for case in report.cases:
            if case.status in {"defect", "adapter-requirement"}:
                lines.append(f"  {case.name}: {case.status}: {case.message}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one or more QT3 XPath diagnostics."""
    parser = argparse.ArgumentParser(
        prog="tests.xpath.qt3",
        description="Run pinned QT3 XPath diagnostics against elementpath.",
    )
    parser.add_argument(
        "--versions",
        nargs="+",
        choices=("2.0", "3.0", "3.1"),
        default=["3.1"],
        help="XPath versions to report (default: 3.1).",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help=f"QT3 corpus directory or catalog (default: {CORPUS_PATH}/catalog.xml).",
    )
    parser.add_argument(
        "--test-set",
        action="append",
        dest="test_sets",
        default=None,
        metavar="NAME",
        help="Test-set name to include (repeatable; default: the declared diagnostic sets).",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON report.")
    arguments = parser.parse_args(argv)
    test_sets = tuple(arguments.test_sets) if arguments.test_sets else QT3_TEST_SETS
    reports = [
        run_qt3_diagnostics(version, corpus=arguments.corpus, test_sets=test_sets)
        for version in arguments.versions
    ]
    if arguments.json:
        sys.stdout.write(render_json(reports))
    else:
        sys.stdout.write(render_text(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
