"""Contract tests for the XPath substrate capability matrix.

These tests describe the installed elementpath 5.1.4 behavior.  A passing
parse is never treated as evidence that evaluation works: the matrix only
records ``pass`` after an expression has been compiled and evaluated.
"""

from __future__ import annotations

import inspect
import json
import runpy
import sys
from copy import copy
from pathlib import Path
from threading import Barrier
from typing import Any

import elementpath
import pytest
from elementpath import XPathContext
from elementpath.xpath_tokens import XPathFunction

import pyxsd.xpath.capabilities as capabilities
from pyxsd.xpath.capabilities import (
    CAPABILITY_STATUSES,
    CapabilityOutcome,
    run_xpath_capabilities,
)

REQUIRED_CAPABILITIES = {
    "maps",
    "arrays",
    "inline-closure",
    "named-reference",
    "dynamic-call",
    "partial-application",
    "schema-constructor",
    "copied-context",
    "nested-predicates",
    "closure-focus",
    "concurrent-invocation",
    "absolute-path-non-node-context",
    "function-call-mutation",
    "context-copy-aliasing",
    "direct-function-call",
}


def test_function_item_isolation() -> None:
    outcomes = {item.name: item for item in run_xpath_capabilities("3.1")}
    assert "partial-application" in outcomes
    assert "inline-closure" in outcomes
    assert outcomes["partial-application"].status in {
        "pass",
        "defect",
        "adapter-requirement",
        "untested",
    }


def test_capability_matrix_covers_every_required_case() -> None:
    outcomes = run_xpath_capabilities("3.1")

    assert {outcome.name for outcome in outcomes} == REQUIRED_CAPABILITIES | {
        "schema-defined-constructor"
    }
    assert all(outcome.status in CAPABILITY_STATUSES for outcome in outcomes)
    assert len({outcome.name for outcome in outcomes}) == len(outcomes)


def test_capability_matrix_records_evaluated_function_items(
    xpath31_outcomes: dict[str, CapabilityOutcome],
) -> None:
    for name in (
        "maps",
        "arrays",
        "inline-closure",
        "named-reference",
        "dynamic-call",
        "partial-application",
        "schema-constructor",
        "copied-context",
        "nested-predicates",
        "concurrent-invocation",
    ):
        outcome = xpath31_outcomes[name]
        assert outcome.status == "pass", (name, outcome.detail)
        assert outcome.evidence, name
        assert outcome.evaluator == "elementpath"
        assert outcome.evaluator_version == elementpath.__version__
        assert outcome.xpath_version == "3.1"
        assert outcome.parser == "XPath31Parser"
        assert outcome.parser_version == "3.1"


def test_capability_matrix_records_source_confirmed_substrate_limits(
    xpath31_outcomes: dict[str, CapabilityOutcome],
) -> None:
    mutation = xpath31_outcomes["function-call-mutation"]
    assert mutation.status == "adapter-requirement"
    assert "is_reference" in mutation.detail
    assert any("is_reference" in evidence for evidence in mutation.evidence)

    copying = xpath31_outcomes["context-copy-aliasing"]
    assert copying.status == "adapter-requirement"
    assert "variables" in copying.detail
    assert any("variables" in evidence for evidence in copying.evidence)

    direct = xpath31_outcomes["direct-function-call"]
    assert direct.status == "adapter-requirement"
    assert "XPDY0002" in direct.detail


def test_absolute_path_with_non_node_context_is_a_substrate_defect(
    xpath31_outcomes: dict[str, CapabilityOutcome],
) -> None:
    outcome = xpath31_outcomes["absolute-path-non-node-context"]

    assert outcome.status == "defect"
    assert "XPTY0020" in outcome.detail
    assert any("XPTY0020" in evidence for evidence in outcome.evidence)


def test_inline_closure_focus_leak_is_a_substrate_defect(
    xpath31_outcomes: dict[str, CapabilityOutcome],
) -> None:
    outcome = xpath31_outcomes["closure-focus"]

    assert outcome.status == "defect"
    assert "XPDY0002" in outcome.detail
    assert any("[5, 6, 7, 8]" in evidence for evidence in outcome.evidence)


def test_schema_defined_constructors_are_untested(
    xpath31_outcomes: dict[str, CapabilityOutcome],
) -> None:
    assert xpath31_outcomes["schema-defined-constructor"].status == "untested"
    assert "schema proxy" in xpath31_outcomes["schema-defined-constructor"].detail


@pytest.mark.parametrize("version", ["2.0", "3.0", "3.1"])
def test_schema_constructor_and_copied_context_run_for_every_version(version: str) -> None:
    outcomes = {item.name: item for item in run_xpath_capabilities(version)}

    assert outcomes["schema-constructor"].status == "pass"
    assert outcomes["copied-context"].status == "pass"
    assert outcomes["nested-predicates"].status == "pass"
    assert outcomes["concurrent-invocation"].status == "pass"
    assert outcomes["absolute-path-non-node-context"].status == "defect"


@pytest.mark.parametrize(
    "version, name, status",
    [
        ("2.0", "maps", "untested"),
        ("2.0", "arrays", "untested"),
        ("2.0", "inline-closure", "untested"),
        ("2.0", "named-reference", "untested"),
        ("2.0", "dynamic-call", "untested"),
        ("2.0", "partial-application", "untested"),
        ("2.0", "function-call-mutation", "untested"),
        ("2.0", "direct-function-call", "untested"),
        ("2.0", "closure-focus", "untested"),
        ("3.0", "closure-focus", "defect"),
        ("3.0", "maps", "untested"),
        ("3.0", "arrays", "untested"),
        ("3.0", "inline-closure", "pass"),
        ("3.0", "named-reference", "pass"),
        ("3.0", "dynamic-call", "pass"),
        ("3.0", "partial-application", "pass"),
        ("3.0", "function-call-mutation", "adapter-requirement"),
        ("3.1", "maps", "pass"),
        ("3.1", "arrays", "pass"),
    ],
)
def test_version_gated_capabilities_are_explicit(version: str, name: str, status: str) -> None:
    outcomes = {item.name: item for item in run_xpath_capabilities(version)}

    assert outcomes[name].status == status, outcomes[name]


@pytest.mark.parametrize(
    "version",
    ["2", "3", "3.1", "2.0", "3.0"],
)
def test_version_aliases_normalise(version: str) -> None:
    outcomes = run_xpath_capabilities(version)

    assert {outcome.xpath_version for outcome in outcomes} == {
        {"2": "2.0", "3": "3.0", "3.1": "3.1", "2.0": "2.0", "3.0": "3.0"}[version]
    }


@pytest.mark.parametrize("version", ["1.0", "4.0", "", "latest"])
def test_unknown_versions_are_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="XPath version"):
        run_xpath_capabilities(version)


def test_capability_outcome_validates_identity_and_status() -> None:
    outcome = CapabilityOutcome("maps", "pass", detail="ok", evidence=("one",))
    assert outcome.evidence == ("one",)

    with pytest.raises(ValueError, match="status"):
        CapabilityOutcome("maps", "maybe")
    with pytest.raises(ValueError, match="name"):
        CapabilityOutcome("", "pass")
    assert outcome.as_dict()["evidence"] == ["one"]


def test_probe_failures_are_classified_without_hiding_them(monkeypatch: pytest.MonkeyPatch) -> None:
    def passed(parser: Any, version: str) -> tuple[str, ...]:
        return ("evaluated",)

    def requirement(parser: Any, version: str) -> tuple[str, ...]:
        raise capabilities.CapabilityRequirement("adapter must copy")

    def untested(parser: Any, version: str) -> tuple[str, ...]:
        raise capabilities.CapabilityUntested("outside the version")

    def defect(parser: Any, version: str) -> tuple[str, ...]:
        raise capabilities.CapabilityDefect("wrong result")

    def crash(parser: Any, version: str) -> tuple[str, ...]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        capabilities,
        "_PROBES",
        (
            ("passed", passed),
            ("requirement", requirement),
            ("untested", untested),
            ("defect", defect),
            ("crash", crash),
        ),
    )
    outcomes = {item.name: item for item in run_xpath_capabilities("3.1")}

    assert outcomes["passed"].status == "pass"
    assert outcomes["requirement"].status == "adapter-requirement"
    assert outcomes["untested"].status == "untested"
    assert outcomes["defect"].status == "defect"
    assert "wrong result" in outcomes["defect"].detail
    assert outcomes["crash"].status == "defect"
    assert "RuntimeError: boom" in outcomes["crash"].detail


def test_parse_errors_are_not_reported_as_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def parse_only(parser: Any, version: str) -> tuple[str, ...]:
        parser.parse("fn:no-such-function()")
        return ()

    monkeypatch.setattr(capabilities, "_PROBES", (("parse-error", parse_only),))

    outcome = run_xpath_capabilities("3.1")[0]

    assert outcome.status == "defect"
    assert "no-such-function" in outcome.detail


def test_assertion_helpers_report_false_conditions() -> None:
    with pytest.raises(capabilities.CapabilityDefect, match="required"):
        capabilities._require(False, "required condition")
    with pytest.raises(capabilities.CapabilityDefect, match="did not raise XPTY0004"):
        capabilities._expect_error(capabilities._parser_for("3.1"), "1 + 2", "XPTY0004")
    with pytest.raises(ValueError, match="at least one outcome"):
        capabilities.build_report("3.1", ())


def test_run_threaded_aborts_peers_when_a_worker_fails() -> None:
    def failing(index: int, barrier: Barrier) -> Any:
        if index == 0:
            raise RuntimeError("worker failed")
        barrier.wait()

    with pytest.raises(RuntimeError, match="worker failed"):
        capabilities._run_threaded(failing)


class _FakeAst:
    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error

    def evaluate(self, context: XPathContext) -> Any:
        if self.error is not None:
            raise self.error
        return self.value


class _FakePathParser:
    """A parser double that raises XPTY0020 for a rooted path."""

    def __init__(self, code: str = "XPTY0020") -> None:
        self.code = code

    def parse(self, expression: str) -> _FakeAst:
        if expression == "/root":
            error = RuntimeError("rooted path")
            error.code = self.code  # type: ignore[attr-defined]
            return _FakeAst(error=error)
        return _FakeAst({} if "map" in expression else [])


def test_absolute_path_probe_passes_when_xpty0020_is_raised() -> None:
    evidence = capabilities._probe_absolute_path_non_node_context(_FakePathParser(), "3.0")

    assert len(evidence) == 2
    assert all("XPTY0020" in item for item in evidence)


def test_absolute_path_probe_rejects_the_wrong_error_code() -> None:
    with pytest.raises(capabilities.CapabilityDefect, match="XPTY0004"):
        capabilities._probe_absolute_path_non_node_context(_FakePathParser("XPTY0004"), "3.0")


def test_closure_focus_probe_passes_when_xpdy0002_is_raised() -> None:
    class Parser:
        def parse(self, expression: str) -> _FakeAst:
            error = RuntimeError("absent focus")
            error.code = "XPDY0002"  # type: ignore[attr-defined]
            return _FakeAst(error=error)

    evidence = capabilities._probe_closure_focus(Parser(), "3.1")

    assert "absent context" in evidence[0]


def test_context_copy_probe_passes_when_copy_isolates_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    monkeypatch.setattr(capabilities, "copy", deepcopy)

    evidence = capabilities._probe_context_copy_aliasing(capabilities._parser_for("3.1"), "3.1")

    assert "isolated" in evidence[0]


def test_function_call_mutation_probe_passes_for_a_stable_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StableFunction:
        source = "fn:concat#2"

        def is_reference(self) -> bool:
            return True

        def __call__(self, *args: Any, **kwargs: Any) -> str:
            return "".join(str(arg) for arg in args)

    monkeypatch.setattr(capabilities, "_evaluate", lambda *args, **kwargs: StableFunction())

    evidence = capabilities._probe_function_call_mutation(capabilities._parser_for("3.1"), "3.1")

    assert "identity" in evidence[0]


def test_direct_function_call_probe_flags_an_unexpectedly_stable_call() -> None:
    class Function:
        def __call__(self, *args: Any, **kwargs: Any) -> int:
            return 42

    class Ast:
        def evaluate(self, context: XPathContext) -> Function:
            return Function()

    class Parser:
        def parse(self, expression: str) -> Ast:
            return Ast()

    with pytest.raises(capabilities.CapabilityDefect, match="unexpectedly succeeded"):
        capabilities._probe_direct_function_call(Parser(), "3.1")


def test_function_call_mutation_is_visible_in_the_installed_source() -> None:
    assert elementpath.__version__ == "5.1.4"
    source = inspect.getsource(XPathFunction.__call__)

    assert "self.clear()" in source
    assert "self._items.append" in source
    assert "tk.value = self.validated_argument" in source


def test_partial_application_placeholders_mutate_after_invocation() -> None:
    parser = capabilities._parser_for("3.1")
    partial = parser.parse("fn:concat(?, '!')").evaluate(XPathContext(item=""))
    placeholder = partial[0]

    assert partial("a") == "a!"
    assert placeholder.value == "a"
    assert partial("b") == "b!"
    assert placeholder.value == "b"


def test_context_copy_is_shallow_in_the_installed_source() -> None:
    source = inspect.getsource(XPathContext.__copy__)

    assert "obj.__dict__.update(self.__dict__)" in source
    assert "obj.variables = self.variables" in source
    assert "obj.namespaces = self.namespaces" in source


def test_context_copy_aliases_mutable_mappings() -> None:
    original = XPathContext(item="", variables={"v": 1})
    duplicate = copy(original)

    assert duplicate.variables is original.variables
    assert duplicate.namespaces is original.namespaces
    duplicate.variables["v"] = 9
    assert original.variables["v"] == 9


def test_direct_inline_function_invocation_requires_an_explicit_context() -> None:
    parser = capabilities._parser_for("3.1")
    context = XPathContext(item="", variables={"n": 40})
    function = parser.parse("function($x){$x + $n}").evaluate(context)

    assert function(2, context=context) == 42
    with pytest.raises(Exception, match="XPDY0002"):
        function(2)


def test_json_report_names_parser_evaluator_and_outcomes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = capabilities.main(["--version", "3.1", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == 1
    assert report["suite"] == "xpath-capabilities"
    assert report["xpath_version"] == "3.1"
    assert report["parser"] == "XPath31Parser"
    assert report["parser_version"] == "3.1"
    assert report["evaluator"] == "elementpath"
    assert report["evaluator_version"] == elementpath.__version__
    assert report["summary"]["pass"] > 0
    assert report["summary"]["defect"] > 0
    assert {outcome["name"] for outcome in report["outcomes"]} == {
        outcome.name for outcome in run_xpath_capabilities("3.1")
    }


def test_text_report_identifies_every_outcome(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = capabilities.main(["--version", "3.0"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "XPath 3.0" in output
    assert "XPath30Parser" in output
    assert "maps" in output
    assert "untested" in output
    assert "absolute-path-non-node-context" in output


def test_module_entry_point_prints_the_default_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["pyxsd.xpath.capabilities", "--version", "3.1", "--json"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(Path(capabilities.__file__)), run_name="__main__")

    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["xpath_version"] == "3.1"
