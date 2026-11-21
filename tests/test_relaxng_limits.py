"""Instance budgets, adversarial derivatives, and parse concurrency.

Validation work is parse-local: a compiled schema keeps only immutable
defaults, while every parse owns its derivative memo tables, semantic
residuals, and guard.  These tests pin the public budget boundary, prove
expensive pattern shapes stop at a configured budget instead of Python's
recursion limit, and exercise the documented concurrency contract.
"""

from __future__ import annotations

import gc
import io
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import pyxsd.relaxng.validator as validator
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng import (
    RelaxNGDocument,
    RelaxNGLimitError,
    RelaxNGLimits,
    RelaxNGSchema,
)
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.limits import BudgetCounter
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Element,
    Empty,
    Group,
    List,
    NotAllowed,
    PatternPool,
    Ref,
    Text,
)

NS = "http://relaxng.org/ns/structure/1.0"

SIMPLE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="record">
      <optional><element name="count"><text/></element></optional>
    </element>
  </start>
</grammar>"""

INVALID_XML = "<record><other/></record>"
VALID_XML = "<record/>"

#: Deep enough that the former recursive derivative path exceeded Python's
#: default recursion limit, but small enough to compile in a normal test run.
DEEP_DEPTH = 1200
#: An integrated event parse keeps one event per authored child; a moderate
#: depth pins the full traversal without quadratic test cost.
INTEGRATED_EVENT_DEPTH = 80
#: Text derivatives nest through their first operand, so a depth just past the
#: old recursion failure proves the iterative path at low test cost.
DEEP_TEXT_DEPTH = 500
#: A direct deep event derivative pins the iterative treatment past Python's
#: recursion limit without replaying that depth for every child occurrence.
ADVERSARIAL_DEPTH = 1200
#: A recursive element tree exercises the iterative document walk, not the
#: pattern derivative depth.
DEEP_DOCUMENT_DEPTH = 1500


def compile_schema(source: str = SIMPLE_SCHEMA, **options) -> RelaxNGSchema:
    return RelaxNGSchema.compile(io.StringIO(source), **options)


def parse(schema: RelaxNGSchema, xml: str, **options) -> RelaxNGDocument:
    return schema.parse(io.StringIO(xml), typed=False, **options)


def deep_text_schema(depth: int = DEEP_TEXT_DEPTH) -> RelaxNGSchema:
    inner = "<text/>"
    for _ in range(depth):
        inner = f"<group>{inner}<text/></group>"
    source = f'<grammar xmlns="{NS}"><start><element name="r">{inner}</element></start></grammar>'
    return compile_schema(source)


def deep_first_operand(tag: str, depth: int = DEEP_DEPTH) -> tuple[RelaxNGSchema, str]:
    inner = '<element name="a0"><empty/></element>'
    for index in range(1, depth + 1):
        inner = f'<{tag}>{inner}<element name="a{index}"><empty/></element></{tag}>'
    source = f'<grammar xmlns="{NS}"><start><element name="r">{inner}</element></start></grammar>'
    children = "".join(f"<a{index}/>" for index in range(depth + 1))
    return compile_schema(source), f"<r>{children}</r>"


def root_content(schema: RelaxNGSchema):
    node = schema.grammar.start
    while isinstance(node, Ref):
        node = node.target
    assert isinstance(node, Element)
    return node.pattern


def derivation_context(limits: RelaxNGLimits | None = None) -> validator.DerivationContext:
    return validator.DerivationContext(DatatypeContext(None, {}), PatternPool(), limits)


def deep_attribute_schema(depth: int = DEEP_DEPTH) -> RelaxNGSchema:
    inner = '<attribute name="a0"><text/></attribute>'
    for index in range(1, depth):
        inner = f'<group>{inner}<attribute name="a{index}"><text/></attribute></group>'
    source = f'<grammar xmlns="{NS}"><start><element name="r">{inner}</element></start></grammar>'
    return compile_schema(source)


def recursive_schema() -> RelaxNGSchema:
    source = f"""<grammar xmlns="{NS}">
      <start><ref name="node"/></start>
      <define name="node">
        <element name="n">
          <zeroOrMore><ref name="node"/></zeroOrMore>
        </element>
      </define>
    </grammar>"""
    return compile_schema(source)


def nested_instance(name: str, depth: int) -> str:
    return f"<{name}>" * depth + f"</{name}>" * depth


class UnreadableStream:
    """A readable-looking stream that records and fails every read."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, *args: object) -> str:
        self.reads += 1
        raise AssertionError("instance input was consumed")


def test_compiled_instance_budgets_apply_when_parse_has_no_override() -> None:
    for name in ("max_derivative_states", "max_memo_entries"):
        schema = compile_schema(limits=RelaxNGLimits(**{name: 0}))

        with pytest.raises(RelaxNGLimitError) as caught:
            parse(schema, VALID_XML)

        assert caught.value.stage == "validate"
        assert caught.value.budget == name
        assert caught.value.configured == 0


def test_parse_override_replaces_compiled_defaults_and_none_is_unlimited() -> None:
    derivative_limited = compile_schema(limits=RelaxNGLimits(max_derivative_states=0))
    assert parse(
        derivative_limited,
        VALID_XML,
        limits=RelaxNGLimits(max_memo_entries=10_000),
    ).is_valid

    memo_limited = compile_schema(limits=RelaxNGLimits(max_memo_entries=0))
    assert parse(
        memo_limited,
        VALID_XML,
        limits=RelaxNGLimits(max_derivative_states=10_000),
    ).is_valid


def test_supplied_empty_limits_clears_bounded_memo_and_diagnostics() -> None:
    memo_limited = compile_schema(limits=RelaxNGLimits(max_memo_entries=0))
    with pytest.raises(RelaxNGLimitError) as caught:
        parse(memo_limited, VALID_XML)
    assert caught.value.budget == "max_memo_entries"
    assert parse(memo_limited, VALID_XML, limits=RelaxNGLimits()).is_valid

    diagnostic_limited = compile_schema(limits=RelaxNGLimits(max_diagnostics=0))
    omitted = parse(diagnostic_limited, INVALID_XML)
    assert omitted.is_valid is False
    assert omitted.report.issues == []
    assert omitted.report.diagnostics_complete is False

    supplied = parse(diagnostic_limited, INVALID_XML, limits=RelaxNGLimits())
    assert supplied.is_valid is False
    assert supplied.report.issues
    assert supplied.report.diagnostics_complete is True


def test_parse_override_can_tighten_or_relax_compiled_defaults() -> None:
    schema = compile_schema(limits=RelaxNGLimits(max_derivative_states=1))

    with pytest.raises(RelaxNGLimitError):
        parse(schema, VALID_XML)

    generous = RelaxNGLimits(max_derivative_states=10_000, max_memo_entries=10_000)
    assert parse(schema, VALID_XML, limits=generous).is_valid

    unlimited = compile_schema()
    with pytest.raises(RelaxNGLimitError) as caught:
        parse(unlimited, VALID_XML, limits=RelaxNGLimits(max_derivative_states=0))
    assert caught.value.budget == "max_derivative_states"


def test_instance_budgets_never_mutate_the_compiled_schema() -> None:
    limits = RelaxNGLimits(max_derivative_states=100, max_memo_entries=100)
    schema = compile_schema(limits=limits)
    grammar = schema.grammar
    issues = tuple(schema.report.issues)

    with pytest.raises(RelaxNGLimitError):
        parse(
            schema,
            VALID_XML,
            limits=RelaxNGLimits(max_derivative_states=0, max_memo_entries=0),
        )

    assert schema.limits is limits
    assert schema.limits.max_derivative_states == 100
    assert schema.limits.max_memo_entries == 100
    assert schema.grammar is grammar
    assert tuple(schema.report.issues) == issues
    assert parse(schema, VALID_XML).is_valid


@pytest.mark.parametrize("budget", ["max_derivative_states", "max_memo_entries"])
def test_public_budget_exhaustion_is_a_limit_error_not_a_verdict(budget: str) -> None:
    schema = compile_schema()

    with pytest.raises(RelaxNGLimitError) as caught:
        parse(schema, VALID_XML, limits=RelaxNGLimits(**{budget: 0}))

    assert caught.value.stage == "validate"
    assert caught.value.budget == budget
    assert caught.value.configured == 0
    assert caught.value.observed == 1


def test_unlimited_and_sufficient_bounded_parses_agree() -> None:
    schema = compile_schema()
    generous = RelaxNGLimits(max_derivative_states=10_000, max_memo_entries=10_000)

    for xml in (VALID_XML, INVALID_XML):
        unlimited = parse(schema, xml)
        bounded = parse(schema, xml, limits=generous)
        assert bounded.is_valid == unlimited.is_valid
        assert bounded.report.diagnostics_complete == unlimited.report.diagnostics_complete
        assert [
            (issue.code, issue.message, issue.location, issue.phase)
            for issue in bounded.report.issues
        ] == [
            (issue.code, issue.message, issue.location, issue.phase)
            for issue in unlimited.report.issues
        ]


def test_repeated_parses_keep_schema_state_and_reports_stable() -> None:
    schema = compile_schema()
    schema.report.add_warning("schema advisory", code="schema-advisory", phase="schema")
    grammar = schema.grammar
    schema_issues = tuple(schema.report.issues)
    first = parse(schema, INVALID_XML)
    first_issues = tuple(
        (issue.code, issue.message, issue.location, issue.phase) for issue in first.report.issues
    )
    root_refs = []

    for _ in range(5):
        document = parse(schema, INVALID_XML)
        root_refs.append(weakref.ref(document.root))
        assert document.report is not first.report
        assert (
            tuple(
                (issue.code, issue.message, issue.location, issue.phase)
                for issue in document.report.issues
            )
            == first_issues
        )
        gc.collect()
        assert sum(ref() is not None for ref in root_refs) <= 1

    valid = parse(schema, VALID_XML)
    for _ in range(3):
        repeated = parse(schema, VALID_XML)
        assert repeated.is_valid and valid.is_valid
        assert [
            (issue.code, issue.message, issue.location, issue.phase)
            for issue in repeated.report.issues
        ] == [
            (issue.code, issue.message, issue.location, issue.phase)
            for issue in valid.report.issues
        ]

    assert tuple(schema.report.issues) == schema_issues
    assert schema.grammar is grammar
    assert [issue.code for issue in first.report.issues] == [
        "schema-advisory",
        "unexpected-element",
    ]


def test_derivation_contexts_are_released_after_success_and_limit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references: list[weakref.ReferenceType[validator.DerivationContext]] = []
    original = validator.DerivationContext

    class TrackingContext(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            references.append(weakref.ref(self))

    monkeypatch.setattr(validator, "DerivationContext", TrackingContext)
    schema = compile_schema()

    assert parse(schema, VALID_XML).is_valid
    gc.collect()
    assert references
    assert all(ref() is None for ref in references)

    before = len(references)
    with pytest.raises(RelaxNGLimitError):
        parse(schema, VALID_XML, limits=RelaxNGLimits(max_derivative_states=0))
    gc.collect()
    assert len(references) > before
    assert all(ref() is None for ref in references)


def test_limits_are_validated_before_instance_input_is_read() -> None:
    schema = compile_schema()
    stream = UnreadableStream()

    with pytest.raises(TypeError, match="limits"):
        schema.parse(stream, typed=False, limits=object())  # type: ignore[arg-type]
    assert stream.reads == 0

    with pytest.raises(ValueError, match="max_derivative_states"):
        RelaxNGLimits(max_derivative_states=-1)


def test_guard_is_released_after_limit_and_malformed_input_failures() -> None:
    schema = compile_schema()

    with pytest.raises(RelaxNGLimitError):
        parse(schema, VALID_XML, limits=RelaxNGLimits(max_derivative_states=0))
    assert parse(schema, VALID_XML).is_valid

    with pytest.raises(PyXSDError):
        schema.parse(io.StringIO("<record>"), typed=False)
    assert parse(schema, VALID_XML).is_valid


def test_guard_is_released_when_a_batch_iterator_closes(tmp_path: Path) -> None:
    from pyxsd.batch import DocumentSource

    schema = compile_schema()
    path = tmp_path / "input.xml"
    path.write_text(VALID_XML, encoding="utf-8")
    iterator = schema.iter_parse([DocumentSource("input", path)], errors="report")
    assert next(iterator).status == "valid"
    iterator.close()

    assert parse(schema, VALID_XML).is_valid


@pytest.mark.parametrize("policy", ["report", "raise"])
def test_guard_is_released_after_a_batch_input_failure(tmp_path: Path, policy: str) -> None:
    from pyxsd.batch import BatchParseError, DocumentSource

    schema = compile_schema()
    path = tmp_path / "malformed.xml"
    path.write_text("<record>", encoding="utf-8")
    iterator = schema.iter_parse([DocumentSource("malformed", path)], errors=policy)
    if policy == "raise":
        with pytest.raises(BatchParseError):
            next(iterator)
    else:
        assert next(iterator).status == "input_error"

    assert parse(schema, VALID_XML).is_valid


def test_reentrant_parse_on_the_same_schema_is_rejected_during_input_read() -> None:
    schema = compile_schema()

    class ReentrantStream(io.StringIO):
        def __init__(self, text: str) -> None:
            super().__init__(text)
            self.error: BaseException | None = None
            self.inner_reads = 0
            self.attempted = False

        def read(self, size: int = -1) -> str:
            if not self.attempted:
                self.attempted = True
                inner = UnreadableStream()
                try:
                    schema.parse(inner, typed=False)
                except BaseException as exc:
                    self.error = exc
                self.inner_reads = inner.reads
            return super().read(size)

    stream = ReentrantStream(VALID_XML)
    document = schema.parse(stream, typed=False)

    assert isinstance(stream.error, RuntimeError)
    assert "concurrent" in str(stream.error) and "reentrant" in str(stream.error)
    assert stream.inner_reads == 0
    assert document.is_valid
    assert parse(schema, VALID_XML).is_valid


def test_concurrent_parse_on_one_schema_is_rejected_without_hanging() -> None:
    schema = compile_schema()
    entered = threading.Event()
    release = threading.Barrier(2)
    results: dict[str, object] = {}

    class GateStream(io.StringIO):
        def read(self, size: int = -1) -> str:
            if not entered.is_set():
                entered.set()
                release.wait()
            return super().read(size)

    def holder() -> None:
        try:
            results["document"] = schema.parse(GateStream(VALID_XML), typed=False)
        except BaseException:
            entered.set()
            release.abort()
            raise

    def challenger() -> None:
        entered.wait()
        blocked = UnreadableStream()
        try:
            schema.parse(blocked, typed=False)
        except RuntimeError as exc:
            results["error"] = exc
        except BaseException:
            release.abort()
            raise
        else:
            release.abort()
            raise AssertionError("a concurrent parse on one schema was accepted")
        assert blocked.reads == 0
        release.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        second = pool.submit(challenger)
        first.result()
        second.result()

    document = results["document"]
    assert isinstance(document, RelaxNGDocument)
    assert document.is_valid
    assert isinstance(results["error"], RuntimeError)
    assert parse(schema, VALID_XML).is_valid


def run_threaded(func, *, num_threads: int = 8, iterations: int = 3) -> None:
    """Run workers together; a worker exception aborts the barrier."""
    for _ in range(iterations):
        barrier = threading.Barrier(num_threads)

        def guarded(index: int, _barrier=barrier) -> object:
            try:
                return func(index, _barrier)
            except BaseException:
                _barrier.abort()
                raise

        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [pool.submit(guarded, index) for index in range(num_threads)]
            for future in futures:
                future.result()


def test_separate_schemas_compile_and_parse_concurrently(
    tmp_path: Path,
) -> None:
    source = tmp_path / "schema.rng"
    source.write_text(SIMPLE_SCHEMA, encoding="utf-8")
    instance = tmp_path / "instance.xml"
    instance.write_text(VALID_XML, encoding="utf-8")

    def worker(index: int, barrier: threading.Barrier) -> object:
        del index
        schema = RelaxNGSchema.compile(source)
        barrier.wait()
        document = schema.parse(instance, typed=False)
        assert document.is_valid
        return document.report.issues

    run_threaded(worker)


def test_deep_text_derivatives_do_not_escape_as_recursion_errors() -> None:
    schema = deep_text_schema()

    document = parse(schema, "<r>text</r>")

    assert document.is_valid
    assert document.root._name_ == "r"


@pytest.mark.parametrize("budget", ["max_derivative_states", "max_memo_entries"])
def test_finite_budget_trips_before_the_python_recursion_limit(budget: str) -> None:
    schema = deep_text_schema()

    with pytest.raises(RelaxNGLimitError) as caught:
        parse(schema, "<r>text</r>", limits=RelaxNGLimits(**{budget: 64}))

    assert caught.value.budget == budget
    assert caught.value.configured == 64
    assert caught.value.observed > 64


def test_deep_group_open_derivatives_are_iterative() -> None:
    schema, _instance = deep_first_operand("group", ADVERSARIAL_DEPTH)
    content = root_content(schema)

    residual = validator.start_tag_open_derivative(content, "a0", derivation_context())

    assert type(residual).__name__ == "_After"
    with pytest.raises(RelaxNGLimitError) as caught:
        validator.start_tag_open_derivative(
            content,
            "a0",
            derivation_context(RelaxNGLimits(max_derivative_states=64)),
        )
    assert caught.value.budget == "max_derivative_states"


def test_deep_interleaved_open_derivatives_are_iterative() -> None:
    schema, _instance = deep_first_operand("interleave", ADVERSARIAL_DEPTH)
    content = root_content(schema)

    residual = validator.start_tag_open_derivative(content, "a0", derivation_context())

    assert type(residual).__name__ == "_After"
    with pytest.raises(RelaxNGLimitError) as caught:
        validator.start_tag_open_derivative(
            content,
            "a0",
            derivation_context(RelaxNGLimits(max_derivative_states=100_000, max_memo_entries=64)),
        )
    assert caught.value.budget == "max_memo_entries"


def test_deep_event_parse_is_iterative() -> None:
    schema, instance = deep_first_operand("group", INTEGRATED_EVENT_DEPTH)

    document = parse(schema, instance)

    assert document.is_valid
    assert len(document.root.children) == INTEGRATED_EVENT_DEPTH + 1


def test_deep_recovery_projection_is_iterative() -> None:
    schema = deep_attribute_schema()

    document = parse(schema, "<r/>")

    assert document.is_valid is False
    assert document.report.has_errors
    assert document.root._name_ == "r"


def test_deep_recursive_element_documents_are_iterative() -> None:
    schema = recursive_schema()

    document = parse(schema, nested_instance("n", DEEP_DOCUMENT_DEPTH))

    assert document.is_valid
    depth = 0
    node = document.root
    while node.children:
        node = node.children[0]
        depth += 1
    assert depth == DEEP_DOCUMENT_DEPTH - 1


def test_empty_instance_limit_stream_is_never_read_when_a_schema_is_invalid() -> None:
    broken = compile_schema(f'<grammar xmlns="{NS}"/>')
    stream = UnreadableStream()

    with pytest.raises(PyXSDError):
        broken.parse(stream, typed=False, limits=RelaxNGLimits(max_derivative_states=0))

    assert stream.reads == 0


def test_deep_patterns_are_not_mistaken_for_invalid_documents() -> None:
    schema = deep_text_schema()
    instance = "<r><unexpected/></r>"

    unlimited = parse(schema, instance)
    bounded = parse(
        schema,
        instance,
        limits=RelaxNGLimits(max_derivative_states=10_000, max_memo_entries=10_000),
    )

    assert unlimited.is_valid is False
    assert bounded.is_valid is False
    assert [issue.code for issue in unlimited.report.issues] == [
        issue.code for issue in bounded.report.issues
    ]
    assert bounded.root.to_etree().tag == "r"


def test_deep_events_match_regular_validation_for_a_plain_tree() -> None:
    schema = recursive_schema()
    instance = ET.fromstring("<n><n/></n>")

    document = schema.parse(instance, typed=False)

    assert document.is_valid
    assert document.root._name_ == "n"


def test_cyclic_derivatives_are_rejected_instead_of_recursing() -> None:
    text_cycle = Ref()
    text_cycle.bind(Group(text_cycle, Text()))
    with pytest.raises(TypeError, match="cyclic RELAX NG text derivative"):
        validator.text_derivative(text_cycle, "x", derivation_context())

    event_cycle = Ref()
    event_cycle.bind(Group(event_cycle, Element(Name(None, "a"), Empty())))
    with pytest.raises(TypeError, match="cyclic RELAX NG event derivative"):
        validator.start_tag_open_derivative(event_cycle, "a", derivation_context())


@pytest.mark.parametrize(
    "limits",
    [None, RelaxNGLimits(max_derivative_states=1, max_memo_entries=1)],
)
def test_list_mediated_reference_cycles_are_rejected_without_recursion(
    limits: RelaxNGLimits | None,
) -> None:
    cycle = Ref()
    cycle.bind(List(cycle))
    context = derivation_context(limits)

    with pytest.raises(TypeError, match="cyclic RELAX NG text derivative"):
        validator.text_derivative(cycle, "a", context)
    assert not context._active_text
    assert not context._active_list

    with pytest.raises(TypeError, match="cyclic RELAX NG text derivative"):
        validator.list_derivative(
            cycle,
            validator.xml_whitespace_tokens("a b"),
            context,
        )
    assert not context._active_text
    assert not context._active_list

    with pytest.raises(TypeError, match="cyclic RELAX NG text derivative"):
        validator.list_derivative(
            List(cycle),
            validator.xml_whitespace_tokens("a"),
            context,
        )
    assert not context._active_text
    assert not context._active_list

    assert validator.text_derivative(Text(), "a", context) == Text()
    assert parse(recursive_schema(), nested_instance("n", 20)).is_valid


def test_apply_after_shares_continuations_without_reworking_or_rejecting_empty_leaves() -> None:
    shared = validator._After(Text(), Empty())
    pattern = Choice(shared, shared)

    result = validator._apply_after(lambda operand: operand, pattern)

    assert result == validator._After(Text(), Empty())
    assert validator._apply_after(lambda operand: operand, NotAllowed()) == NotAllowed()


def test_recovery_projection_reuses_shared_memo_and_shared_operands() -> None:
    ref = Ref()
    ref.bind(Text())
    memo: dict[int, object] = {}
    first = validator._without_attributes(ref, memo)
    second = validator._without_attributes(ref, memo)
    assert first == second == Text()

    shared = validator._After(Attribute(Name(None, "a"), Text()), Empty())
    expected = validator._without_attributes(shared)
    result = validator._without_attributes(Group(shared, shared))
    assert result == Group(expected, expected)


def test_compact_budgets_default_to_none_and_reject_invalid_values() -> None:
    limits = RelaxNGLimits()

    assert limits.max_compact_tokens is None
    assert limits.max_compact_depth is None
    for name in ("max_compact_tokens", "max_compact_depth"):
        for bad in (True, -1, 1.5, "2"):
            with pytest.raises(ValueError, match=name):
                RelaxNGLimits(**{name: bad})


def test_observe_accepts_the_configured_depth_and_raises_one_above() -> None:
    counter = BudgetCounter("compile", RelaxNGLimits(max_compact_depth=3))

    counter.observe("max_compact_depth", 3)
    counter.observe("max_compact_depth", 0)
    with pytest.raises(RelaxNGLimitError) as caught:
        counter.observe("max_compact_depth", 4)

    assert (
        caught.value.stage,
        caught.value.budget,
        caught.value.configured,
        caught.value.observed,
    ) == ("compile", "max_compact_depth", 3, 4)


def test_observe_is_a_high_water_check_not_a_cumulative_charge() -> None:
    counter = BudgetCounter(
        "compile",
        RelaxNGLimits(max_compact_tokens=4, max_compact_depth=2),
    )

    counter.observe("max_compact_depth", 2)
    counter.observe("max_compact_depth", 2)
    for _ in range(4):
        counter.consume("max_compact_tokens")
    with pytest.raises(RelaxNGLimitError) as caught:
        counter.consume("max_compact_tokens")

    assert caught.value.observed == 5


def test_observe_without_a_configured_budget_has_no_bound() -> None:
    BudgetCounter("compile", None).observe("max_compact_depth", 10_000)
    BudgetCounter("compile", RelaxNGLimits()).observe("max_compact_depth", 10_000)


def test_observe_rejects_an_unknown_budget_name() -> None:
    with pytest.raises(ValueError, match="unknown"):
        BudgetCounter("compile", RelaxNGLimits()).observe("max_nodes", 1)


@pytest.mark.parametrize("observed", [True, False, -1, 1.0, "1"])
def test_observe_rejects_invalid_depths(observed: object) -> None:
    with pytest.raises(ValueError, match="observed"):
        BudgetCounter("compile", RelaxNGLimits()).observe(
            "max_compact_depth",
            observed,  # type: ignore[arg-type]
        )
