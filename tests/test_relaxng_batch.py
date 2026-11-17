"""Batch parsing contracts for validation-only RELAX NG schemas.

The batch orchestration is shared with XSD: caller-labelled local paths,
lazy caller-order evaluation, expected input failures, duplicate-id
rejection, and file lifetimes.  RELAX NG adds only the schema adapter, so
these tests pin the shared behavior without changing XSD batch semantics.
"""

from __future__ import annotations

import builtins
import gc
import io
import weakref
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pyxsd.batch import BatchParseError, DocumentSource
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.relaxng import RelaxNGDocument, RelaxNGLimitError, RelaxNGLimits, RelaxNGSchema
from pyxsd.validation import IssueSeverity

NS = "http://relaxng.org/ns/structure/1.0"

SIMPLE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="record">
      <optional><element name="count"><text/></element></optional>
    </element>
  </start>
</grammar>"""

BROKEN_SCHEMA = f"""<grammar xmlns="{NS}"/>"""


def compile_schema(source: str = SIMPLE_SCHEMA, **options) -> RelaxNGSchema:
    return RelaxNGSchema.compile(io.StringIO(source), **options)


def sources_for(tmp_path: Path, *texts: str) -> list[DocumentSource]:
    sources = []
    for index, text in enumerate(texts):
        path = tmp_path / f"{index}.xml"
        path.write_text(text, encoding="utf-8")
        sources.append(DocumentSource(str(index), path))
    return sources


@pytest.fixture
def schema() -> RelaxNGSchema:
    return compile_schema()


@pytest.fixture
def mixed_sources(tmp_path: Path) -> list[DocumentSource]:
    return sources_for(
        tmp_path,
        "<record><count>1</count></record>",
        "<record><other/></record>",
        "<record>",
        "<record><count>4</count></record>",
    )


def test_report_mode_isolates_each_input_and_retains_lexical_documents(
    schema: RelaxNGSchema, mixed_sources: list[DocumentSource]
) -> None:
    outcomes = list(schema.iter_parse(mixed_sources, errors="report"))

    assert [outcome.status for outcome in outcomes] == [
        "valid",
        "invalid",
        "input_error",
        "valid",
    ]
    valid, invalid, malformed, later = outcomes
    assert valid.issues == later.issues == ()
    assert isinstance(valid.document, RelaxNGDocument)
    assert isinstance(invalid.document, RelaxNGDocument)
    assert invalid.issues and all(issue.phase == "instance" for issue in invalid.issues)
    assert invalid.document.root._name_ == "record"
    assert invalid.document.root.children[0]._name_ == "other"
    assert malformed.document is None
    assert malformed.error is not None
    assert malformed.error.kind == "malformed_xml"
    assert malformed.issues == ()
    assert later.document.root._name_ == "record"
    assert type(valid.document.root) is type(later.document.root)


def test_report_mode_yields_documents_that_can_revalidate_during_iteration(
    schema: RelaxNGSchema, mixed_sources: list[DocumentSource]
) -> None:
    iterator = schema.iter_parse(mixed_sources, errors="report")
    first = next(iterator)
    assert first.status == "valid"
    assert first.document is not None

    fresh = first.document.revalidate()

    assert fresh is not first.document
    assert fresh.is_valid
    assert next(iterator).status == "invalid"
    iterator.close()


def test_raise_mode_stops_without_pulling_later_sources(
    schema: RelaxNGSchema, mixed_sources: list[DocumentSource]
) -> None:
    pulled: list[str] = []

    def inputs():
        for source in mixed_sources:
            pulled.append(source.id)
            yield source

    results = schema.iter_parse(inputs())
    assert next(results).status == "valid"

    with pytest.raises(BatchParseError) as caught:
        next(results)

    assert caught.value.outcome.source.id == "1"
    assert caught.value.outcome.status == "invalid"
    assert isinstance(caught.value.__cause__, ValidationError)
    assert pulled == ["0", "1"]


@pytest.mark.parametrize(
    "text,kind,cause",
    [(None, "io", OSError), ("<record>", "malformed_xml", ET.ParseError)],
)
@pytest.mark.parametrize("policy", ["report", "raise"])
def test_expected_input_failures_are_isolated_or_stop_the_batch(
    schema: RelaxNGSchema,
    tmp_path: Path,
    text: str | None,
    kind: str,
    cause: type[BaseException],
    policy: str,
) -> None:
    path = tmp_path / "input.xml"
    if text is not None:
        path.write_text(text, encoding="utf-8")

    iterator = schema.iter_parse([DocumentSource("input", path)], errors=policy)
    if policy == "raise":
        with pytest.raises(BatchParseError) as caught:
            next(iterator)
        assert isinstance(caught.value.__cause__, PyXSDError)
        assert isinstance(caught.value.__cause__.__cause__, cause)
        outcome = caught.value.outcome
    else:
        outcome = next(iterator)

    assert outcome.status == "input_error"
    assert outcome.error is not None
    assert outcome.error.kind == kind
    assert outcome.error.message
    assert outcome.document is None
    assert outcome.issues == ()
    assert all(not isinstance(value, BaseException) for value in vars(outcome.error).values())


def test_invalid_schema_is_checked_before_any_source_is_pulled(tmp_path: Path) -> None:
    schema = compile_schema(BROKEN_SCHEMA)
    assert schema.report.has_errors
    pulled: list[bool] = []

    def inputs():
        pulled.append(True)
        yield DocumentSource("input", tmp_path / "missing.xml")

    with pytest.raises(ValidationError):
        list(schema.iter_parse(inputs(), errors="report"))

    assert pulled == []


def test_duplicate_ids_stop_before_opening_but_the_same_path_is_allowed(
    schema: RelaxNGSchema, mixed_sources: list[DocumentSource]
) -> None:
    source = mixed_sources[0]
    results = schema.iter_parse([source, DocumentSource(source.id, "missing.xml")], errors="report")
    assert next(results).status == "valid"
    with pytest.raises(ValueError, match="duplicate"):
        next(results)

    assert [
        outcome.status
        for outcome in schema.iter_parse(
            [source, DocumentSource("again", source.path)], errors="report"
        )
    ] == ["valid", "valid"]


def test_invalid_error_policy_and_source_types_are_rejected(schema: RelaxNGSchema) -> None:
    with pytest.raises(ValueError, match="errors"):
        list(schema.iter_parse([], errors="ignore"))
    with pytest.raises(TypeError, match="DocumentSource"):
        list(schema.iter_parse(["input.xml"], errors="report"))


@pytest.mark.parametrize("policy", ["report", "raise"])
def test_source_iterator_exceptions_propagate(schema: RelaxNGSchema, policy: str) -> None:
    def inputs():
        raise RuntimeError("iterator defect")
        yield

    with pytest.raises(RuntimeError, match="iterator defect"):
        list(schema.iter_parse(inputs(), errors=policy))


@pytest.mark.parametrize("policy", ["report", "raise"])
def test_limit_failures_are_not_turned_into_input_errors(tmp_path: Path, policy: str) -> None:
    schema = compile_schema(limits=RelaxNGLimits(max_derivative_states=0))
    sources = sources_for(tmp_path, "<record/>")

    with pytest.raises(RelaxNGLimitError) as caught:
        next(schema.iter_parse(sources, errors=policy))

    assert caught.value.budget == "max_derivative_states"
    assert caught.value.stage == "validate"


def test_no_prefetch_or_consumption_after_close(
    schema: RelaxNGSchema, mixed_sources: list[DocumentSource]
) -> None:
    pulled: list[str] = []

    def inputs():
        for source in mixed_sources:
            pulled.append(source.id)
            yield source

    iterator = schema.iter_parse(inputs(), errors="report")
    assert pulled == []
    assert next(iterator).status == "valid"
    assert pulled == ["0"]

    iterator.close()

    assert pulled == ["0"]
    assert list(iterator) == []


@pytest.mark.parametrize("text", ["<record><count>1</count></record>", "<record>"])
@pytest.mark.parametrize("policy", ["report", "raise"])
def test_files_are_closed_before_yield_or_raise(
    schema: RelaxNGSchema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    policy: str,
) -> None:
    sources = sources_for(tmp_path, text)
    opened = []
    original = builtins.open

    def track(*args, **kwargs):
        stream = original(*args, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(builtins, "open", track)
    iterator = schema.iter_parse(sources, errors=policy)
    if text == "<record>" and policy == "raise":
        with pytest.raises(BatchParseError) as caught:
            next(iterator)
        assert caught.value.outcome.error is not None
        assert caught.value.outcome.error.kind == "malformed_xml"
    else:
        outcome = next(iterator)
        if outcome.document is not None:
            assert outcome.document.source == sources[0].path

    assert opened
    assert all(stream.closed for stream in opened)
    iterator.close()


def test_many_batch_parses_do_not_accumulate_documents(
    schema: RelaxNGSchema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = sources_for(tmp_path, *(["<record><count>1</count></record>"] * 40))
    root_refs = []
    original = RelaxNGSchema.parse

    def track_parse(owner, xml, **options):
        document = original(owner, xml, **options)
        root_refs.append(weakref.ref(document.root))
        return document

    monkeypatch.setattr(RelaxNGSchema, "parse", track_parse)
    iterator = schema.iter_parse(inputs)
    for _ in inputs:
        next(iterator)
        gc.collect()
        assert sum(ref() is not None for ref in root_refs) <= 1
    iterator.close()
    gc.collect()
    assert not any(ref() is not None for ref in root_refs)


def test_batch_outcomes_preserve_absolute_sources_and_schema_issues(
    schema: RelaxNGSchema, tmp_path: Path
) -> None:
    schema.report.add_warning("schema advisory", code="schema-advisory", phase="schema")
    schema_issues = tuple(schema.report.issues)
    grammar = schema.grammar
    limits = schema.limits
    source = DocumentSource("relative", tmp_path / "input.xml")
    source.path.write_text("<record/>", encoding="utf-8")

    outcome = next(schema.iter_parse([source]))
    after = next(schema.iter_parse([source]))

    assert outcome.status == "valid"
    assert outcome.source.path.is_absolute()
    assert outcome.document is not None
    assert outcome.document.source == source.path
    assert [(issue.code, issue.severity) for issue in outcome.issues] == [
        ("schema-advisory", IssueSeverity.WARNING)
    ]
    assert [issue.code for issue in after.issues] == ["schema-advisory"]
    assert tuple(schema.report.issues) == schema_issues
    assert schema.grammar is grammar
    assert schema.limits is limits
