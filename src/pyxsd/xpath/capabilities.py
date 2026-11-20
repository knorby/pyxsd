"""Measure the installed elementpath XPath substrate.

The report is a diagnostic, not a conformance claim.  Every capability is
compiled *and evaluated* independently; a successful parse never produces a
``pass``.  Outcomes separate four cases:

``pass``
    The installed substrate produced the value required by the probe.

``defect``
    The substrate ran but contradicted a required XPath behavior (or failed
    to parse an expression that the requested language version requires).

``adapter-requirement``
    The substrate is usable, but the XSLT/XPath adapter must accommodate an
    observed limitation (for example, cloning a mutable function token before
    each invocation).

``untested``
    The requested version does not contain the feature, or exercising it
    requires a facility outside this probe (such as a schema proxy).

Use ``python -m pyxsd.xpath.capabilities --version 3.1`` for a human-readable
matrix and ``--json`` for a machine-readable report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass
from threading import Barrier
from typing import Any

import elementpath
from elementpath import XPathContext
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath3 import XPath30Parser, XPath31Parser

#: Statuses a capability outcome may use.
CAPABILITY_STATUSES = frozenset({"pass", "defect", "adapter-requirement", "untested"})

#: Report schema version.
REPORT_SCHEMA = 1

_VERSION_ORDER = {"2.0": (2, 0), "3.0": (3, 0), "3.1": (3, 1)}
_VERSION_ALIASES = {
    "2": "2.0",
    "2.0": "2.0",
    "3": "3.0",
    "3.0": "3.0",
    "3.1": "3.1",
}
_PARSERS: dict[str, Any] = {
    "2.0": XPath2Parser,
    "3.0": XPath30Parser,
    "3.1": XPath31Parser,
}


@dataclass(frozen=True)
class CapabilityOutcome:
    """The result of one independent capability probe."""

    name: str
    status: str
    detail: str = ""
    evidence: tuple[str, ...] = ()
    xpath_version: str = ""
    parser: str = ""
    parser_version: str = ""
    evaluator: str = "elementpath"
    evaluator_version: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a capability outcome requires a name")
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError(f"unknown capability status: {self.status!r}")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready representation with stable field names."""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "xpath_version": self.xpath_version,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
        }


class CapabilityRequirement(Exception):
    """A substrate limitation the XSLT/XPath adapter must accommodate."""

    def __init__(self, message: str, evidence: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.evidence = tuple(evidence)


class CapabilityUntested(Exception):
    """The requested version or the available environment cannot run a probe."""

    def __init__(self, message: str, evidence: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.evidence = tuple(evidence)


class CapabilityDefect(Exception):
    """The substrate contradicted a required XPath behavior."""


def _normalise_version(version: str) -> str:
    normalized = _VERSION_ALIASES.get(version.strip())
    if normalized is None:
        raise ValueError(f"unknown XPath version: {version!r}")
    return normalized


def _parser_for(version: str) -> Any:
    return _PARSERS[_normalise_version(version)]()


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", "") or ""
    return code.rsplit(":", 1)[-1]


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return [value]


def _evaluate(
    parser: Any,
    expression: str,
    *,
    context: XPathContext | None = None,
    variables: dict[str, Any] | None = None,
) -> Any:
    if context is None:
        context = XPathContext(item="", variables=variables)
    return parser.parse(expression).evaluate(context)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityDefect(message)


def _require_items(actual: Any, expected: list[Any], label: str) -> None:
    values = _items(actual)
    _require(values == expected, f"{label}: expected {expected!r}, got {values!r}")


def _expect_error(
    parser: Any,
    expression: str,
    expected_code: str,
    *,
    context: XPathContext | None = None,
) -> str:
    try:
        _evaluate(parser, expression, context=context)
    except Exception as exc:
        actual = _error_code(exc)
        _require(
            actual == expected_code,
            f"{expression!r} raised {actual or type(exc).__name__}, expected {expected_code}",
        )
        return actual
    raise CapabilityDefect(f"{expression!r} did not raise {expected_code}")


def _at_least(version: str, minimum: str) -> bool:
    return _VERSION_ORDER[version] >= _VERSION_ORDER[minimum]


def _run_threaded(function: Callable[[int, Barrier], Any], num_threads: int = 8) -> list[Any]:
    """Run *function* in threads released together, aborting the barrier on error."""
    barrier = Barrier(num_threads)

    def guarded(index: int) -> Any:
        try:
            return function(index, barrier)
        except BaseException:
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(guarded, index) for index in range(num_threads)]
        return [future.result() for future in futures]


# ---------------------------------------------------------------------------
# Independent probes


def _probe_maps(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.1"):
        raise CapabilityUntested(f"XPath {version} has no map type")
    size = _evaluate(parser, "map:size(map{'a': 1, 'b': (2, 3)})")
    _require(size == 2, f"map:size returned {size!r}, expected 2")
    _require_items(
        _evaluate(parser, "map:get(map{'a': 1, 'b': (2, 3)}, 'b')"),
        [2, 3],
        "map:get",
    )
    _require(
        _evaluate(parser, "map:contains(map{'a': 1}, 'c')") is False,
        "map:contains returned true for an absent key",
    )
    merged = _evaluate(parser, "map:size(map:merge((map{'a': 1}, map{'b': 2})))")
    _require(merged == 2, f"map:merge size returned {merged!r}, expected 2")
    keys = _items(_evaluate(parser, "map:keys(map{'a': 1, 'b': 2})"))
    _require(sorted(str(key) for key in keys) == ["a", "b"], f"map:keys returned {keys!r}")
    duplicate = _expect_error(parser, "map{'a': 1, 'a': 2}", "XQDY0137")
    return (f"map size/get/contains/merge/keys evaluated; duplicate keys raised {duplicate}",)


def _probe_arrays(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.1"):
        raise CapabilityUntested(f"XPath {version} has no array type")
    size = _evaluate(parser, "array:size([1, 2, 3])")
    _require(size == 3, f"array:size returned {size!r}, expected 3")
    member = _evaluate(parser, "array:get([1, 2, 3], 2)")
    _require(member == 2, f"array:get returned {member!r}, expected 2")
    sequence_size = _evaluate(parser, "array:size([(1, 2), 3])")
    _require(sequence_size == 2, f"array member sequences merged: size {sequence_size!r}")
    _require_items(_evaluate(parser, "array:get([(1, 2), 3], 1)"), [1, 2], "array member")
    _require_items(_evaluate(parser, "array:flatten([1, [2, 3]])"), [1, 2, 3], "array:flatten")
    out_of_bounds = _expect_error(parser, "array:get([1, 2], 3)", "FOAY0001")
    return (f"array size/get/flatten evaluated; out-of-bounds raised {out_of_bounds}",)


def _probe_inline_closure(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no inline function syntax")
    _require(
        _evaluate(parser, "(function($x){$x + 1})(2)") == 3, "inline call returned wrong value"
    )
    nested = _evaluate(parser, "(function($x){function($y){$x + $y}})(2)(3)")
    _require(nested == 5, f"nested inline closure returned {nested!r}, expected 5")
    context = XPathContext(item="", variables={"n": 40})
    function = parser.parse("function($x){$x + $n}").evaluate(context)
    captured = function(2, context=context)
    _require(captured == 42, f"captured closure returned {captured!r}, expected 42")
    return ("inline and nested closures evaluated; lexical variable captured",)


def _probe_named_reference(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no named function references")
    name = _evaluate(parser, "local-name-from-QName(function-name(fn:concat#2))")
    _require(name == "concat", f"function-name returned {name!r}, expected concat")
    arity = _evaluate(parser, "function-arity(fn:concat#2)")
    _require(arity == 2, f"function-arity returned {arity!r}, expected 2")
    called = _evaluate(parser, "(fn:concat#2)('a', 'b')")
    _require(called == "ab", f"named reference returned {called!r}, expected ab")
    looked_up = _evaluate(
        parser,
        "fn:function-lookup("
        "fn:QName('http://www.w3.org/2005/xpath-functions', 'concat'), 2)('a', 'b')",
    )
    _require(looked_up == "ab", f"function-lookup returned {looked_up!r}, expected ab")
    _require_items(
        _evaluate(
            parser,
            "fn:function-lookup("
            "fn:QName('http://www.w3.org/2005/xpath-functions', 'no-such-function'), 1)",
        ),
        [],
        "missing function-lookup",
    )
    return ("name/arity/dynamic call/function-lookup evaluated",)


def _probe_dynamic_call(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no function items")
    value = _evaluate(parser, "(function($x){$x * 2})(3)")
    _require(value == 6, f"dynamic call returned {value!r}, expected 6")
    selected = _evaluate(parser, "(if (true()) then upper-case#1 else lower-case#1)('x')")
    _require(selected == "X", f"conditional function item returned {selected!r}, expected X")
    if _at_least(version, "3.1"):
        mapped = _evaluate(parser, "(map{'f': function($x){$x + 1}}?f)(2)")
        _require(mapped == 3, f"function item from a map returned {mapped!r}, expected 3")
    return ("inline, conditional, and mapped function items invoked dynamically",)


def _probe_partial_application(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no partial application")
    first = _evaluate(parser, "(fn:concat(?, '!'))('a')")
    _require(first == "a!", f"partial application returned {first!r}, expected a!")
    second = _evaluate(parser, "(fn:concat(?, '-', ?))('a', 'b')")
    _require(second == "a-b", f"two-placeholder partial returned {second!r}, expected a-b")
    arity = _evaluate(parser, "function-arity(fn:concat(?, '!'))")
    _require(arity == 1, f"partial arity returned {arity!r}, expected 1")
    partial = parser.parse("fn:concat(?, '!')").evaluate(XPathContext(item=""))
    _require(partial("a") == "a!", "reused partial returned the wrong first result")
    _require(partial("b") == "b!", "reused partial returned the wrong second result")
    too_many = _expect_error(parser, "(fn:concat(?, '-', ?))('a', 'b', 'c')", "XPTY0004")
    return (f"partial application evaluated and reused; arity error raised {too_many}",)


def _probe_schema_constructor(parser: Any, version: str) -> tuple[str, ...]:
    integer = _evaluate(parser, "xs:integer('42')")
    _require(integer == 42, f"xs:integer returned {integer!r}, expected 42")
    date = _evaluate(parser, "xs:date('2020-01-01')")
    _require(str(date) == "2020-01-01", f"xs:date returned {date!r}")
    decimal = _evaluate(parser, "xs:decimal('1.5')")
    _require(decimal == 1.5, f"xs:decimal returned {decimal!r}, expected 1.5")
    invalid = _expect_error(parser, "xs:integer('nope')", "FORG0001")
    return (f"built-in constructors evaluated; invalid lexical form raised {invalid}",)


def _probe_schema_defined_constructor(parser: Any, version: str) -> tuple[str, ...]:
    raise CapabilityUntested(
        "user-defined schema constructors require a schema proxy; "
        "built-in constructors are covered by schema-constructor"
    )


def _probe_copied_context(parser: Any, version: str) -> tuple[str, ...]:
    original = XPathContext(item="", variables={"v": 1})
    duplicate = copy(original)
    value = _evaluate(parser, "$v + 1", context=duplicate)
    _require(value == 2, f"copied context evaluated to {value!r}, expected 2")
    _require(duplicate.item == original.item, "copied context did not preserve the item")
    return ("copied context preserved item and variables for evaluation",)


def _probe_context_copy_aliasing(parser: Any, version: str) -> tuple[str, ...]:
    original = XPathContext(item="", variables={"v": 1})
    duplicate = copy(original)
    if (
        duplicate.variables is not original.variables
        or duplicate.namespaces is not original.namespaces
    ):
        return ("XPathContext.__copy__ isolated mutable mappings",)
    duplicate.variables["v"] = 9
    _require(
        original.variables["v"] == 9,
        "XPathContext.__copy__ aliased mappings without sharing changes",
    )
    raise CapabilityRequirement(
        "XPathContext.__copy__ shallow-copies variables and namespaces; "
        "the adapter must clone mutable mappings before scoped evaluation",
        evidence=(
            "copy(ctx).variables is ctx.variables",
            "mutating the copy changed the original variable value",
        ),
    )


def _probe_direct_function_call(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no function items")
    context = XPathContext(item="", variables={"n": 40})
    function = parser.parse("function($x){$x + $n}").evaluate(context)
    _require(
        function(2, context=context) == 42, "direct call with a context returned the wrong value"
    )
    try:
        function(2)
    except Exception as exc:
        code = _error_code(exc)
        _require(
            code == "XPDY0002", f"direct call without a context raised {code or type(exc).__name__}"
        )
        raise CapabilityRequirement(
            "direct invocation of an inline function requires an explicit context "
            "(XPDY0002 without one); the adapter must pass its evaluation context",
            evidence=("function(2) raised XPDY0002", "function(2, context=ctx) returned 42"),
        ) from exc
    raise CapabilityDefect("direct invocation without a context unexpectedly succeeded")


def _probe_function_call_mutation(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no function items")
    function = _evaluate(parser, "fn:concat#2")
    reference_before = function.is_reference()
    _require(reference_before, "a named function reference did not report is_reference()")
    name_before = function.as_function().__name__
    _require(
        name_before == "<fn:concat#2>",
        f"named function reference exposed the wrong wrapper name: {name_before!r}",
    )
    result = function("a", "b")
    _require(result == "ab", f"named function reference returned {result!r}, expected ab")
    name_after = function.as_function().__name__
    if function.is_reference():
        return ("named function reference retained its token identity after invocation",)
    raise CapabilityRequirement(
        "XPathFunction.__call__ mutates the reusable function token: "
        "is_reference() changed from True to False, the token keeps invocation arguments, "
        f"and as_function().__name__ changed from {name_before!r} to {name_after!r}; "
        "the adapter must clone or recreate callable tokens for each invocation",
        evidence=(
            f"as_function().__name__ changed from {name_before!r} to {name_after!r}",
            f"source changed from {function.source!r} after invocation",
            "is_reference() changed from True to False",
        ),
    )


def _probe_nested_predicates(parser: Any, version: str) -> tuple[str, ...]:
    _require_items(
        _evaluate(parser, "(1 to 10)[. mod 2 = 0][. > 4]"),
        [6, 8, 10],
        "nested numeric predicates",
    )
    _require_items(
        _evaluate(parser, "(10 to 20)[position() = 2][. = 11]"),
        [11],
        "nested positional predicates",
    )
    _require_items(
        _evaluate(parser, "(1 to 5)[. = (2, 4)]"),
        [2, 4],
        "predicate value sequence",
    )
    if _at_least(version, "3.0"):
        _require_items(
            _evaluate(parser, "(10 to 12)[(function(){ position() })() = 2]"),
            [11],
            "contextual call inside a predicate",
        )
    return ("nested numeric, positional, and contextual predicates evaluated",)


def _probe_concurrent_invocation(parser: Any, version: str) -> tuple[str, ...]:
    if _at_least(version, "3.0"):
        compiled = parser.parse("(function($x){$x * 2})(21)")
        expected = [42]
    else:
        compiled = parser.parse("count((1 to 200)[. mod 7 = 0])")
        expected = [28]
    baseline = _items(compiled.evaluate(XPathContext(item="")))
    _require(
        baseline == expected,
        f"compiled expression baseline returned {baseline!r}, expected {expected!r}",
    )

    def evaluate(_: int, barrier: Barrier) -> list[Any]:
        barrier.wait()
        return _items(compiled.evaluate(XPathContext(item="")))

    results = _run_threaded(evaluate)
    _require(
        results == [expected] * len(results),
        f"concurrent evaluation diverged: {results!r}",
    )
    return (f"{len(results)} simultaneous evaluations of one compiled expression agreed",)


def _probe_closure_focus(parser: Any, version: str) -> tuple[str, ...]:
    if not _at_least(version, "3.0"):
        raise CapabilityUntested(f"XPath {version} has no inline function syntax")
    expression = "(1 to 4) ! (let $add := function($x){$x + .} return $add(4))"
    try:
        value = _evaluate(parser, expression)
    except Exception as exc:
        code = _error_code(exc)
        _require(
            code == "XPDY0002",
            f"closure focus probe raised {code or type(exc).__name__}, expected XPDY0002",
        )
        return ("an inline closure correctly rejected the absent context item",)
    raise CapabilityDefect(
        "an inline closure captured the outer context item and returned "
        f"{_items(value)!r} instead of raising XPDY0002"
    )


def _probe_absolute_path_non_node_context(parser: Any, version: str) -> tuple[str, ...]:
    contexts: list[tuple[str, XPathContext]] = [
        ("string", XPathContext(item="atomic")),
        ("integer", XPathContext(item=1)),
    ]
    if _at_least(version, "3.1"):
        contexts.append(("map", XPathContext(item=_evaluate(parser, "map{'a': 1}"))))
        contexts.append(("array", XPathContext(item=_evaluate(parser, "[1, 2]"))))

    evidence: list[str] = []
    for label, context in contexts:
        try:
            value = parser.parse("/root").evaluate(context)
        except Exception as exc:
            code = _error_code(exc)
            _require(
                code == "XPTY0020",
                f"absolute path with a {label} context item raised {code or type(exc).__name__}, "
                "expected XPTY0020",
            )
            evidence.append(f"{label}: raised XPTY0020")
            continue
        raise CapabilityDefect(
            f"absolute path with a {label} context item returned {_items(value)!r} "
            "instead of raising XPTY0020"
        )
    return tuple(evidence)


_PROBES: tuple[tuple[str, Callable[[Any, str], tuple[str, ...]]], ...] = (
    ("maps", _probe_maps),
    ("arrays", _probe_arrays),
    ("inline-closure", _probe_inline_closure),
    ("named-reference", _probe_named_reference),
    ("dynamic-call", _probe_dynamic_call),
    ("partial-application", _probe_partial_application),
    ("schema-constructor", _probe_schema_constructor),
    ("schema-defined-constructor", _probe_schema_defined_constructor),
    ("copied-context", _probe_copied_context),
    ("context-copy-aliasing", _probe_context_copy_aliasing),
    ("direct-function-call", _probe_direct_function_call),
    ("function-call-mutation", _probe_function_call_mutation),
    ("nested-predicates", _probe_nested_predicates),
    ("closure-focus", _probe_closure_focus),
    ("concurrent-invocation", _probe_concurrent_invocation),
    ("absolute-path-non-node-context", _probe_absolute_path_non_node_context),
)


def _run(
    name: str,
    probe: Callable[[Any, str], tuple[str, ...]],
    version: str,
    parser_class: Any,
) -> CapabilityOutcome:
    parser = parser_class()
    metadata = {
        "xpath_version": version,
        "parser": parser_class.__name__,
        "parser_version": getattr(parser_class, "version", ""),
        "evaluator": "elementpath",
        "evaluator_version": elementpath.__version__,
    }
    try:
        evidence = probe(parser, version)
    except CapabilityRequirement as exc:
        return CapabilityOutcome(
            name,
            "adapter-requirement",
            str(exc),
            exc.evidence,
            **metadata,
        )
    except CapabilityUntested as exc:
        return CapabilityOutcome(name, "untested", str(exc), exc.evidence, **metadata)
    except CapabilityDefect as exc:
        return CapabilityOutcome(name, "defect", str(exc), (str(exc),), **metadata)
    except Exception as exc:  # a substrate exception is a defect, never a pass
        detail = f"{type(exc).__name__}: {exc}"
        return CapabilityOutcome(name, "defect", detail, (detail,), **metadata)
    return CapabilityOutcome(name, "pass", "evaluated expressions", evidence, **metadata)


def run_xpath_capabilities(version: str) -> tuple[CapabilityOutcome, ...]:
    """Run every capability probe independently for one XPath version.

    ``version`` accepts ``"2.0"``, ``"3.0"`` and ``"3.1"`` (plus the short
    aliases ``"2"`` and ``"3"``).  Each probe is isolated: one failure never
    prevents the remaining capabilities from being measured.
    """
    normalized = _normalise_version(version)
    parser_class = _PARSERS[normalized]
    return tuple(_run(name, probe, normalized, parser_class) for name, probe in _PROBES)


def build_report(version: str, outcomes: Sequence[CapabilityOutcome]) -> dict[str, Any]:
    """Build the deterministic capability report dictionary."""
    if not outcomes:
        raise ValueError("a capability report requires at least one outcome")
    first = outcomes[0]
    summary = Counter(outcome.status for outcome in outcomes)
    return {
        "schema": REPORT_SCHEMA,
        "suite": "xpath-capabilities",
        "xpath_version": first.xpath_version,
        "parser": first.parser,
        "parser_version": first.parser_version,
        "evaluator": first.evaluator,
        "evaluator_version": first.evaluator_version,
        "summary": dict(sorted(summary.items())),
        "outcomes": [outcome.as_dict() for outcome in outcomes],
    }


def render_json(version: str, outcomes: Sequence[CapabilityOutcome]) -> str:
    """Render the capability report as deterministic JSON."""
    return json.dumps(build_report(version, outcomes), indent=2, sort_keys=True) + "\n"


def render_text(version: str, outcomes: Sequence[CapabilityOutcome]) -> str:
    """Render the capability report as a compact table."""
    report = build_report(version, outcomes)
    width = max(len(outcome.name) for outcome in outcomes)
    summary = ", ".join(f"{status}={count}" for status, count in report["summary"].items())
    lines = [
        f"XPath {report['xpath_version']} capability matrix "
        f"({report['parser']} {report['parser_version']}, "
        f"elementpath {report['evaluator_version']})",
        summary,
        "",
    ]
    lines.extend(
        f"{outcome.name:<{width}}  {outcome.status:<20}  {outcome.detail}" for outcome in outcomes
    )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the capability matrix and print it in text or JSON form."""
    parser = argparse.ArgumentParser(
        prog="pyxsd.xpath.capabilities",
        description="Measure the installed elementpath XPath substrate.",
    )
    parser.add_argument(
        "--version",
        choices=("2.0", "3.0", "3.1"),
        default="3.1",
        help="XPath version to measure (default: 3.1).",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON report.")
    arguments = parser.parse_args(argv)
    outcomes = run_xpath_capabilities(arguments.version)
    if arguments.json:
        sys.stdout.write(render_json(arguments.version, outcomes))
    else:
        sys.stdout.write(render_text(arguments.version, outcomes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
