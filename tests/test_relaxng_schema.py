"""The public RELAX NG compilation entry point and immutable schema identity.

A compiled :class:`~pyxsd.relaxng.schema.RelaxNGSchema` owns one report, one
composed semantic graph, one complete dependency closure, and one stable
datatype-registry identity.  Grammatical problems are schema findings; only
unreadable/malformed input and unsupported capabilities interrupt compilation.
Typed instance parsing is a later phase and is rejected explicitly before any
input is consumed.
"""

from __future__ import annotations

import hashlib
import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType
from xml.etree import ElementTree as ET

import pytest

import pyxsd
import pyxsd.relaxng as relaxng
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.locations import ConstraintInfo, IssueLocation
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    Datatype,
    DatatypeContext,
    DatatypeLibraryRegistration,
    DatatypeRegistrationError,
    UnsupportedDatatypeError,
)
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    OneOrMore,
    Pattern,
    Ref,
    Text,
    nullable,
)
from pyxsd.relaxng.schema import RelaxNGSchema, _SchemaReport
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.validation import ValidationReport
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

NS = "http://relaxng.org/ns/structure/1.0"

VALID = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <attribute name="id"><text/></attribute>
      <optional><element name="item"><text/></element></optional>
    </element>
  </start>
</grammar>"""

CUSTOM = f"""<grammar xmlns="{NS}">
  <start>
    <element name="x">
      <data type="thing" datatypeLibrary="urn:custom"/>
    </element>
  </start>
</grammar>"""

XSD_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string"/>
</xs:schema>"""

BROKEN_XSD_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="a">
    <xs:complexType>
      <xs:complexContent>
        <xs:extension base="NoSuchType"/>
      </xs:complexContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

EXPECTED_START = Element(
    Name(None, "doc"),
    Group(
        Attribute(Name(None, "id"), Text()),
        Choice(Empty(), Element(Name(None, "item"), Text())),
    ),
)


class OpaqueDatatype(Datatype):
    """A custom datatype with validation but no codecs."""

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del context
        return lexical == "ok"

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        del left_context, right_context
        return left == right


class OpaqueFactory:
    """A factory that returns one observable datatype instance."""

    def __init__(self) -> None:
        self.datatype = OpaqueDatatype()

    def create(
        self,
        type_name: str,
        parameters: tuple[object, ...],
        context: DatatypeContext,
    ) -> Datatype:
        del type_name, parameters, context
        return self.datatype


class CountingStream(io.StringIO):
    """A readable stream that records whether compilation consumed it."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.reads = 0

    def read(self, *args: object) -> str:
        self.reads += 1
        return super().read(*args)  # type: ignore[arg-type]


class ScopePolicy:
    """A policy that records compilation-scope entry and schema reads."""

    def __init__(self, payload: str = VALID) -> None:
        self.payload = payload
        self.events: list[tuple[object, ...]] = []
        self.reads: list[tuple[str, str]] = []

    @contextmanager
    def compilation(self, *, root_read: bool = True) -> Iterator[None]:
        self.events.append(("enter", root_read))
        try:
            yield
        finally:
            self.events.append(("exit",))

    _pyxsd_compilation_scope = compilation

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        del base
        self.reads.append((source, purpose))
        return ResourceData(source, self.payload.encode("utf-8"))

    def parse(self, data: ResourceData, context: object) -> ET.Element:
        return parse_with_namespaces(io.BytesIO(data.content), context)  # type: ignore[arg-type]


def compile_schema(
    source: object = None,
    **options: object,
) -> RelaxNGSchema:
    """Compile the shared valid schema unless another source is supplied."""
    return RelaxNGSchema.compile(
        io.StringIO(VALID) if source is None else source,  # type: ignore[arg-type]
        **options,  # type: ignore[arg-type]
    )


def deref(pattern: object) -> object:
    """Follow bound element reference edges to their target."""
    while isinstance(pattern, Ref):
        pattern = pattern.target
    return pattern


def resolve(pattern: object) -> object:
    """Rebuild a bound graph with every reference edge replaced by its target."""
    pattern = deref(pattern)
    if isinstance(pattern, (Choice, Group, Interleave)):
        return replace(pattern, first=resolve(pattern.first), second=resolve(pattern.second))
    if isinstance(pattern, (Element, Attribute, List, OneOrMore)):
        return replace(pattern, pattern=resolve(pattern.pattern))
    return pattern


def registration(factory: OpaqueFactory, fingerprint: str | None) -> DatatypeLibraryRegistration:
    """Build a custom registration, including deliberately invalid ones."""
    return DatatypeLibraryRegistration(factory, fingerprint)  # type: ignore[arg-type]


def issue_key(report: ValidationReport) -> list[tuple[object, ...]]:
    return [(issue.severity, issue.code, issue.message, issue.location) for issue in report.issues]


def codes(report: ValidationReport) -> list[str]:
    return [issue.code for issue in report.issues]


# ----------------------------------------------------------------------
# Lifecycle and immutable identity


def test_compile_returns_the_valid_composed_schema_with_an_empty_report():
    schema = compile_schema()

    assert isinstance(schema, RelaxNGSchema)
    assert schema.report.has_errors is False
    assert schema.report.warning_count == 0
    assert schema.report.issues == []
    assert schema.report.suppressed_count == 0
    schema.require_valid()

    assert resolve(schema.grammar.start) == EXPECTED_START
    assert isinstance(schema.grammar.definitions, MappingProxyType)
    assert len(schema.grammar.definitions) == 2
    assert all(
        isinstance(definition.pattern, Element)
        for definition in schema.grammar.definitions.values()
    )
    assert len(schema.grammar.graph) == 9
    assert schema.limits == RelaxNGLimits()
    assert schema.namespace_context.root_bindings == {"": NS}
    assert schema.datatype_fingerprint.startswith("pyxsd.relaxng.datatypes/1:")


def test_a_bare_pattern_root_compiles_like_a_grammar_start():
    schema = compile_schema(io.StringIO(f'<element xmlns="{NS}" name="x"><empty/></element>'))

    assert not schema.report.has_errors
    assert resolve(schema.grammar.start) == Element(Name(None, "x"), Empty())


@pytest.mark.parametrize("construct", ["element", "group", "choice", "mixed"])
def test_deep_authored_patterns_compile_to_deterministic_canonical_graphs(construct):
    depth = 1100
    if construct == "element":
        body = '<element name="node">' * depth + "<empty/>" + "</element>" * depth
    elif construct == "mixed":
        body = (
            '<element name="node"><group><text/><choice><empty/>' * depth
            + "<text/>"
            + "</choice></group></element>" * depth
        )
    elif construct == "group":
        body = f"<{construct}><text/>" * depth + "<text/>" + f"</{construct}>" * depth
        body = f'<element name="root">{body}</element>'
    else:
        body = (
            "".join(
                f'<choice><element name="child{index}"><empty/></element>' for index in range(depth)
            )
            + "<text/>"
            + "</choice>" * depth
        )
        body = f'<element name="root">{body}</element>'
    xml = f'<grammar xmlns="{NS}"><start>{body}</start></grammar>'
    signatures = []
    for _ in range(2):
        schema = compile_schema(io.StringIO(xml))
        assert not schema.report.has_errors
        compiled = schema.grammar
        pending = [compiled.start, *(item.pattern for item in compiled.definitions.values())]
        seen = set()
        signature = []
        while pending:
            node = pending.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            assert compiled.graph.intern(node) is node
            signature.append((type(node).__name__, nullable(node)))
            if construct == "choice" and isinstance(node, Choice):
                assert nullable(node) is isinstance(node.second, Text)
            else:
                assert nullable(node) is isinstance(node, (Empty, Text, Group, Choice))
            if isinstance(node, Ref):
                assert compiled.graph.intern(node.target) is node.target
                pending.append(node.target)
            elif isinstance(node, (Choice, Group, Interleave)):
                pending.extend((node.second, node.first))
            elif isinstance(node, Element):
                pending.append(node.pattern)
        signatures.append(signature)
        assert not nullable(compiled.start)
        expected_definitions = {"element": depth, "mixed": depth, "group": 1, "choice": depth + 1}
        assert len(compiled.definitions) == expected_definitions[construct]
        if construct == "group":
            assert sum(kind == "Group" for kind, _ in signature) == depth
        elif construct == "choice":
            assert sum(kind == "Choice" for kind, _ in signature) == depth
            assert nullable(deref(compiled.start).pattern)
        elif construct == "element":
            assert len(compiled.graph) == depth * 2 + 1
        else:
            assert sum(kind == "Choice" for kind, _ in signature) == depth
    assert signatures[0] == signatures[1]


def test_every_final_ref_and_definition_uses_the_final_pool_identity():
    schema = compile_schema(
        io.StringIO(f"""<grammar xmlns="{NS}">
      <start><ref name="a"/></start>
      <define name="a"><element name="a"><choice>
        <empty/><ref name="b"/><ref name="b"/>
      </choice></element></define>
      <define name="b"><element name="b"><optional><ref name="a"/></optional>
      </element></define>
    </grammar>""")
    )
    assert not schema.report.has_errors
    compiled = schema.grammar
    pending: list[Pattern] = [
        compiled.start,
        *(item.pattern for item in compiled.definitions.values()),
    ]
    seen = set()
    refs = []
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        assert compiled.graph.intern(node) is node
        assert compiled.graph.origins(node)
        if isinstance(node, Ref):
            refs.append(node)
            assert compiled.graph.intern(node.target) is node.target
            pending.append(node.target)
        elif isinstance(node, Choice):
            pending.extend((node.first, node.second))
        elif isinstance(node, Element):
            pending.append(node.pattern)
    assert len(refs) == 2
    assert {id(ref.target) for ref in refs} == {
        id(item.pattern) for item in compiled.definitions.values()
    }
    a = deref(compiled.start)
    b = deref(a.pattern.second)
    assert b.pattern.second.target is a
    assert any(
        origin.location is not None
        and any(step.name == f"{{{NS}}}ref" for step in origin.location.path)
        for origin in compiled.graph.origins(b)
    )


def test_compile_accepts_binary_streams():
    schema = RelaxNGSchema.compile(io.BytesIO(VALID.encode("utf-8")))

    assert not schema.report.has_errors
    assert resolve(schema.grammar.start) == EXPECTED_START


def test_compiled_schema_attributes_are_read_only():
    schema = compile_schema()

    for attribute in (
        "report",
        "grammar",
        "dependencies",
        "namespace_context",
        "limits",
        "datatype_fingerprint",
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(schema, attribute, None)


def test_stream_dependencies_name_the_memory_source_and_hash_its_bytes():
    schema = compile_schema()

    assert isinstance(schema.dependencies, tuple)
    assert schema.dependencies == schema.grammar.dependencies
    assert len(schema.dependencies) == 1
    dependency = schema.dependencies[0]
    assert dependency.canonical_id == "<memory>"
    assert dependency.source_id == "<memory>"
    assert dependency.purpose == "schema"
    assert dependency.content_sha256 == hashlib.sha256(VALID.encode("utf-8")).hexdigest()
    assert dependency.references == ()


def test_recompiling_one_schema_is_deterministic_but_not_one_identity():
    first = compile_schema()
    second = compile_schema()

    assert first is not second
    assert first.report.issues == second.report.issues
    assert resolve(first.grammar.start) == resolve(second.grammar.start)
    assert {
        name: resolve(definition.pattern) for name, definition in first.grammar.definitions.items()
    } == {
        name: resolve(definition.pattern) for name, definition in second.grammar.definitions.items()
    }
    assert first.dependencies == second.dependencies
    assert first.datatype_fingerprint == second.datatype_fingerprint
    assert first.namespace_context.root_bindings == second.namespace_context.root_bindings


def test_namespace_context_keeps_the_exact_root_frame():
    xml = (
        f'<r:grammar xmlns:r="{NS}" xmlns="" xmlns:p="urn:example:p">'
        '<r:start><r:element name="root"><r:empty/></r:element></r:start>'
        "</r:grammar>"
    )

    schema = compile_schema(io.StringIO(xml))

    assert schema.namespace_context.root_bindings == {"r": NS, "": "", "p": "urn:example:p"}


def test_independent_schemas_compile_concurrently():
    barrier = threading.Barrier(2)
    results: dict[str, RelaxNGSchema] = {}
    failures: list[BaseException] = []

    def work(name: str, xml: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = RelaxNGSchema.compile(io.StringIO(xml))
        except BaseException as exc:
            failures.append(exc)
            barrier.abort()

    one = f'<grammar xmlns="{NS}"><start><element name="one"><empty/></element></start></grammar>'
    two = f'<grammar xmlns="{NS}"><start><element name="two"><empty/></element></start></grammar>'
    threads = [
        threading.Thread(target=work, args=("one", one)),
        threading.Thread(target=work, args=("two", two)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert set(results) == {"one", "two"}
    assert deref(results["one"].grammar.start) == Element(Name(None, "one"), Empty())
    assert deref(results["two"].grammar.start) == Element(Name(None, "two"), Empty())


# ----------------------------------------------------------------------
# Reports and require_valid


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            '<start><ref name="missing"/></start>',
            "relaxng-undefined-reference",
        ),
        (
            '<define name="only"><empty/></define>',
            "relaxng-missing-start",
        ),
        (
            '<start><attribute name="a"><empty/></attribute></start>',
            "relaxng-start-contains-attribute",
        ),
    ],
)
def test_grammatical_problems_are_schema_findings_not_exceptions(body, code):
    schema = compile_schema(io.StringIO(f'<grammar xmlns="{NS}">{body}</grammar>'))

    assert schema.report.has_errors
    assert code in codes(schema.report)
    assert all(issue.phase == "schema" for issue in schema.report.issues)
    assert all(issue.location is not None for issue in schema.report.issues)

    with pytest.raises(ValidationError) as excinfo:
        schema.require_valid()
    assert excinfo.value.report is schema.report
    assert "not valid" in str(excinfo.value)


def test_reader_and_simplifier_findings_merge_once_into_one_report():
    xml = f'<grammar xmlns="{NS}"><start><element name="x"><unknown/></element></start></grammar>'

    schema = compile_schema(io.StringIO(xml))

    assert codes(schema.report).count("relaxng-unknown-element") == 1
    assert codes(schema.report).count("relaxng-unsupported-pattern") == 1
    keys = issue_key(schema.report)
    assert len(keys) == len(set(keys))
    assert all(issue.phase == "schema" for issue in schema.report.issues)
    assert schema.report.error_count == len(schema.report.issues)
    assert schema.report.suppressed_count == 0
    reader_issue = next(
        issue for issue in schema.report.issues if issue.code == "relaxng-unknown-element"
    )
    assert reader_issue.location is not None
    assert reader_issue.location.source == "<memory>"
    assert reader_issue.location.path[-1].name.endswith("}unknown")


def test_finding_order_is_deterministic_across_compilations():
    xml = (
        f'<grammar xmlns="{NS}">'
        '<start><element name="x"><unknown/><other/></element></start>'
        "</grammar>"
    )

    first = compile_schema(io.StringIO(xml))
    second = compile_schema(io.StringIO(xml))

    assert issue_key(first.report) == issue_key(second.report)


def test_distinct_findings_with_one_code_are_all_retained():
    xml = (
        f'<grammar xmlns="{NS}">'
        '<start><element name="x"><unknown/><other/></element></start>'
        "</grammar>"
    )

    schema = compile_schema(io.StringIO(xml))

    reader_issues = [
        issue for issue in schema.report.issues if issue.code == "relaxng-unknown-element"
    ]
    assert len(reader_issues) == 2
    assert len({issue.location for issue in reader_issues}) == 2


def test_duplicate_include_findings_are_reported_once(tmp_path):
    child = tmp_path / "child.rng"
    child.write_text(
        f'<grammar xmlns="{NS}">'
        '<define name="item"><element name="x"><unknown/></element></define>'
        "</grammar>",
        encoding="utf-8",
    )
    main = tmp_path / "main.rng"
    main.write_text(
        f'<grammar xmlns="{NS}">'
        '<include href="child.rng"/>'
        '<include href="child.rng"/>'
        '<start><ref name="item"/></start>'
        "</grammar>",
        encoding="utf-8",
    )

    schema = RelaxNGSchema.compile(main)

    assert codes(schema.report).count("relaxng-unknown-element") == 1
    assert codes(schema.report).count("relaxng-unsupported-pattern") == 1
    assert codes(schema.report).count("relaxng-duplicate-define") == 1
    keys = issue_key(schema.report)
    assert len(keys) == len(set(keys))


def test_dependency_closure_covers_the_root_and_every_authored_reference(tmp_path):
    child = tmp_path / "child.rng"
    child.write_text(
        f'<grammar xmlns="{NS}">'
        '<define name="item"><element name="item"><empty/></element></define>'
        "</grammar>",
        encoding="utf-8",
    )
    main = tmp_path / "main.rng"
    main.write_text(
        f'<grammar xmlns="{NS}">'
        '<include href="child.rng"/>'
        '<start><ref name="item"/></start>'
        "</grammar>",
        encoding="utf-8",
    )

    schema = RelaxNGSchema.compile(main)

    assert not schema.report.has_errors
    by_id = {dependency.canonical_id: dependency for dependency in schema.dependencies}
    root_id = str(main.resolve())
    child_id = str(child.resolve())
    assert set(by_id) == {root_id, child_id}
    assert by_id[root_id].content_sha256 == hashlib.sha256(main.read_bytes()).hexdigest()
    assert by_id[root_id].references == ()
    included = by_id[child_id]
    assert included.content_sha256 == hashlib.sha256(child.read_bytes()).hexdigest()
    assert [(edge.containing_id, edge.reference, edge.base) for edge in included.references] == [
        (root_id, "child.rng", root_id)
    ]


# ----------------------------------------------------------------------
# Input failures, policy trust, and options


def test_malformed_and_unreadable_input_raise_pyxsd_error(tmp_path):
    with pytest.raises(PyXSDError, match="could not be opened"):
        RelaxNGSchema.compile(tmp_path / "missing.rng")
    with pytest.raises(PyXSDError, match="not well-formed"):
        RelaxNGSchema.compile(io.StringIO(f'<grammar xmlns="{NS}">'))


def test_a_foreign_root_is_a_schema_finding_not_a_read_error():
    schema = RelaxNGSchema.compile(io.StringIO("<html/>"))

    assert schema.report.has_errors
    assert "relaxng-root-not-relaxng" in codes(schema.report)


def test_remote_sources_require_an_authorizing_policy():
    with pytest.raises(ResourcePolicyError, match="remote"):
        RelaxNGSchema.compile("https://example.invalid/schema.rng")


@pytest.mark.parametrize(
    "extra",
    [
        {"xml_policy": XmlParserPolicy.default()},
        {"xml_limits": XmlLimits(max_bytes=1)},
    ],
)
def test_resource_and_xml_options_cannot_be_combined(extra, tmp_path):
    stream = CountingStream(VALID)
    with pytest.raises(ValueError, match="cannot be combined"):
        RelaxNGSchema.compile(stream, resource_policy=ScopePolicy(), **extra)
    assert stream.reads == 0

    with pytest.raises(ValueError, match="cannot be combined"):
        RelaxNGSchema.compile(tmp_path / "missing.rng", resource_policy=ScopePolicy(), **extra)


def test_xml_limits_apply_to_the_root_schema_parse():
    with pytest.raises(ResourcePolicyError, match="max_elements"):
        RelaxNGSchema.compile(io.StringIO(VALID), xml_limits=XmlLimits(max_elements=1))


def test_xml_policy_applies_to_the_root_schema_parse():
    xml = (
        "<!DOCTYPE grammar>"
        f'<grammar xmlns="{NS}"><start><element name="x"><empty/></element></start>'
        "</grammar>"
    )

    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        RelaxNGSchema.compile(io.StringIO(xml), xml_policy=XmlParserPolicy.strict())


def test_a_policy_owns_parsing_and_its_compilation_scope():
    policy = ScopePolicy()

    schema = RelaxNGSchema.compile("schema.rng", resource_policy=policy)

    assert policy.reads == [("schema.rng", "schema")]
    assert policy.events == [("enter", True), ("exit",)]
    assert not schema.report.has_errors


def test_an_anonymous_stream_is_not_a_policy_root_read():
    policy = ScopePolicy()

    schema = RelaxNGSchema.compile(io.StringIO(VALID), resource_policy=policy)

    assert policy.reads == []
    assert policy.events == [("enter", False), ("exit",)]
    assert not schema.report.has_errors


def test_limits_default_to_unlimited_and_are_exposed():
    unlimited = compile_schema()
    assert unlimited.limits == RelaxNGLimits()

    limits = RelaxNGLimits(max_pattern_nodes=10_000)
    bounded = compile_schema(limits=limits)
    assert bounded.limits is limits


def test_pattern_node_budget_exhaustion_is_a_limit_error():
    with pytest.raises(RelaxNGLimitError) as excinfo:
        compile_schema(limits=RelaxNGLimits(max_pattern_nodes=0))

    error = excinfo.value
    assert (error.stage, error.budget, error.configured, error.observed) == (
        "compile",
        "max_pattern_nodes",
        0,
        1,
    )


@pytest.mark.parametrize("retained", [0, 1])
def test_max_diagnostics_bounds_report_retention_without_changing_verdict(retained):
    xml = (
        f'<grammar xmlns="{NS}">'
        '<start><element name="x"><unknown/><other/></element></start>'
        "</grammar>"
    )

    schema = compile_schema(io.StringIO(xml), limits=RelaxNGLimits(max_diagnostics=retained))

    assert len(schema.report) == retained
    assert schema.report.error_count == 4
    assert schema.report.warning_count == 0
    assert schema.report.suppressed_count == 4 - retained
    assert schema.report.has_errors
    with pytest.raises(ValidationError):
        schema.require_valid()


@pytest.mark.parametrize("policy_bound", [None, 0, 2])
@pytest.mark.parametrize("diagnostic_bound", [None, 0, 1, 3])
def test_policy_report_factory_preserves_identity_bounds_and_counts(policy_bound, diagnostic_bound):
    class ReportPolicy(ScopePolicy):
        def __init__(self):
            super().__init__()
            self.report = ValidationReport(max_issues=policy_bound, max_message_bytes=12)
            self.calls = 0

        def new_report(self):
            self.calls += 1
            return self.report

    policy = ReportPolicy()
    xml = (
        f'<grammar xmlns="{NS}">'
        '<start><element name="x"><unknown/><other/></element></start>'
        "</grammar>"
    )
    schema = compile_schema(
        io.StringIO(xml),
        resource_policy=policy,
        limits=RelaxNGLimits(max_diagnostics=diagnostic_bound),
    )

    assert policy.calls == 1
    assert schema.report is policy.report
    bounds = [bound for bound in (policy_bound, diagnostic_bound, 4) if bound is not None]
    retained = min(bounds)
    assert len(schema.report) == retained
    assert schema.report.phase == "schema"
    assert schema.report.error_count == 4
    assert schema.report.warning_count == 0
    assert schema.report.suppressed_count == 4 - retained
    assert all(issue.phase == "schema" for issue in schema.report)
    assert all(len(issue.message.encode("utf-8")) <= 12 for issue in schema.report)
    assert all(issue.message.endswith("...") for issue in schema.report)
    with pytest.raises(ValidationError) as excinfo:
        schema.require_valid()
    assert excinfo.value.report is policy.report


@pytest.mark.parametrize("diagnostic_bound", [None, 0, 1, 4])
def test_schema_report_sink_preserves_existing_state_and_deduplicates_both_severities(
    diagnostic_bound,
):
    report = ValidationReport(max_issues=5, max_message_bytes=100)
    report.add_error("existing error", code="existing-error")
    report.add_warning("existing warning", code="existing-warning")
    sink = _SchemaReport(report, diagnostic_bound)
    location = IssueLocation("schema.rng", ())
    constraint = ConstraintInfo("schema", expected="valid", actual="invalid")
    details = {"test": "evidence"}
    for add in (sink.add_error, sink.add_warning):
        for _ in range(2):
            add(
                "unique finding",
                code=add.__name__,
                element="element",
                location=location,
                constraint=constraint,
                details=details,
                location_precision="exact",
            )

    assert report.phase == sink.phase == "schema"
    assert report.error_count == report.warning_count == 2
    assert sink.error_count == sink.warning_count == 1
    retained = 4 if diagnostic_bound is None else diagnostic_bound
    assert len(report) == retained
    assert report.suppressed_count == 4 - retained
    assert report.has_errors
    for issue in report.issues[2:]:
        assert issue.phase == "schema"
        assert issue.element == "element"
        assert issue.location == location
        assert issue.constraint is constraint
        assert issue.details is details
        assert issue.location_precision == "exact"


def test_limits_must_be_a_relaxng_limits_value():
    stream = CountingStream(VALID)

    with pytest.raises(TypeError, match="RelaxNGLimits"):
        RelaxNGSchema.compile(stream, limits={"max_pattern_nodes": 1})  # type: ignore[arg-type]
    assert stream.reads == 0


# ----------------------------------------------------------------------
# Datatype registries and identity


def test_custom_registration_augments_the_builtin_libraries():
    factory = OpaqueFactory()
    libraries = {"urn:custom": registration(factory, "custom/1")}

    schema = RelaxNGSchema.compile(io.StringIO(CUSTOM), datatype_libraries=libraries)

    assert not schema.report.has_errors
    start = deref(schema.grammar.start)
    assert isinstance(start, Element)
    assert isinstance(start.pattern, Data)
    assert start.pattern.datatype is factory.datatype
    assert schema.datatype_fingerprint != compile_schema().datatype_fingerprint


def test_an_unregistered_library_is_an_explicit_capability_failure():
    with pytest.raises(UnsupportedDatatypeError, match="urn:custom"):
        RelaxNGSchema.compile(io.StringIO(CUSTOM))


def test_the_default_registry_has_a_pinned_identity():
    # The identity covers the built-in and XSD library fingerprints in URI
    # order; changing either identity or the digest shape changes this pin.
    assert (
        compile_schema().datatype_fingerprint == "pyxsd.relaxng.datatypes/1:"
        "ddb2e934acbb1e10c8385be990a5b73b42cf18708d3ce93f082a0a2ca51d503d"
    )
    assert compile_schema(datatype_libraries={}).datatype_fingerprint == (
        compile_schema().datatype_fingerprint
    )


def test_the_datatype_fingerprint_ignores_mapping_order():
    forward = {
        "urn:a": registration(OpaqueFactory(), "a/1"),
        "urn:b": registration(OpaqueFactory(), "b/1"),
    }
    backward = dict(reversed(list(forward.items())))

    first = compile_schema(datatype_libraries=forward)
    second = compile_schema(datatype_libraries=backward)

    assert first.datatype_fingerprint == second.datatype_fingerprint


def test_a_nonreserved_registration_may_be_explicitly_overridden():
    first_factory = OpaqueFactory()
    second_factory = OpaqueFactory()
    first = RelaxNGSchema.compile(
        io.StringIO(CUSTOM),
        datatype_libraries={"urn:custom": registration(first_factory, "custom/1")},
    )
    second = RelaxNGSchema.compile(
        io.StringIO(CUSTOM),
        datatype_libraries={"urn:custom": registration(second_factory, "custom/2")},
    )

    assert not first.report.has_errors
    assert not second.report.has_errors
    first_start = deref(first.grammar.start)
    second_start = deref(second.grammar.start)
    assert isinstance(first_start, Element) and isinstance(first_start.pattern, Data)
    assert isinstance(second_start, Element) and isinstance(second_start.pattern, Data)
    assert first_start.pattern.datatype is first_factory.datatype
    assert second_start.pattern.datatype is second_factory.datatype
    assert first.datatype_fingerprint != second.datatype_fingerprint


@pytest.mark.parametrize(
    ("uri", "label"),
    [(BUILTIN_LIBRARY, "built-in"), (XSD_LIBRARY, "XSD")],
)
def test_reserved_datatype_identities_cannot_be_replaced(uri, label):
    stream = CountingStream(VALID)

    with pytest.raises(DatatypeRegistrationError, match=f"reserved {label}"):
        RelaxNGSchema.compile(
            stream,
            datatype_libraries={uri: registration(OpaqueFactory(), "custom/1")},
        )
    assert stream.reads == 0


@pytest.mark.parametrize("fingerprint", [None, "", "   ", "<factory at 0xdeadbeef>"])
def test_custom_registrations_require_a_stable_fingerprint(fingerprint):
    stream = CountingStream(VALID)

    with pytest.raises(DatatypeRegistrationError):
        RelaxNGSchema.compile(
            stream,
            datatype_libraries={"urn:custom": registration(OpaqueFactory(), fingerprint)},
        )
    assert stream.reads == 0


def test_the_datatype_library_mapping_is_validated_before_reading():
    stream = CountingStream(VALID)

    with pytest.raises(TypeError, match="DatatypeLibraryRegistration"):
        RelaxNGSchema.compile(
            stream,
            datatype_libraries={"urn:custom": OpaqueFactory()},  # type: ignore[dict-item]
        )
    with pytest.raises(TypeError, match="URI"):
        RelaxNGSchema.compile(
            stream,
            datatype_libraries={3: registration(OpaqueFactory(), "custom/1")},  # type: ignore[dict-item]
        )
    with pytest.raises(TypeError, match="mapping"):
        RelaxNGSchema.compile(
            stream,
            datatype_libraries=[("urn:custom", registration(OpaqueFactory(), "custom/1"))],  # type: ignore[arg-type]
        )
    assert stream.reads == 0


# ----------------------------------------------------------------------
# Phase boundary and XSD compatibility


def test_typed_parse_is_available_and_validation_only_remains_explicit():
    schema = compile_schema()

    stream = CountingStream('<doc id="x"/>')
    typed_document = schema.parse(stream)
    assert stream.reads > 0
    assert typed_document.typed is True
    assert typed_document.is_valid

    validation_stream = CountingStream('<doc id="x"/>')
    validation_document = schema.parse(validation_stream, typed=False)
    assert validation_stream.reads > 0
    assert validation_document.typed is False
    assert validation_document.is_valid


def test_pattern_capture_is_rejected_before_consuming_input():
    schema = compile_schema()
    stream = CountingStream("<doc/>")

    with pytest.raises(ValueError, match="capture"):
        schema.parse("<doc/>", typed=False, capture="patterns")

    with pytest.raises(ValueError, match="capture"):
        schema.parse(stream, typed=False, capture="patterns")
    assert stream.reads == 0


def test_a_broken_schema_is_rejected_before_instance_input():
    schema = compile_schema(io.StringIO(f'<grammar xmlns="{NS}"/>'))
    assert schema.report.has_errors
    stream = CountingStream("<doc/>")

    with pytest.raises(ValidationError) as excinfo:
        schema.parse(stream, typed=False)

    assert stream.reads == 0
    assert excinfo.value.report is schema.report


def test_the_relaxng_package_exposes_the_real_compile_entry_point():
    assert set(relaxng.__all__) == {
        "CandidateSelection",
        "AlternativeValue",
        "AmbiguousValueError",
        "ListInterpretation",
        "NodeAnnotations",
        "RelaxNGDocument",
        "RelaxNGElement",
        "RelaxNGLimitError",
        "RelaxNGLimits",
        "RelaxNGSchema",
        "ScalarAnnotation",
        "StaleInterpretationError",
        "compile",
    }
    assert relaxng.RelaxNGSchema is RelaxNGSchema
    assert relaxng.compile.__func__ is RelaxNGSchema.compile.__func__
    schema = relaxng.compile(io.StringIO(VALID))
    assert isinstance(schema, RelaxNGSchema)


def test_pyxsd_compile_remains_the_xsd_only_entry_point():
    assert pyxsd.compile.__func__ is pyxsd.Schema.compile.__func__
    assert "RelaxNGSchema" not in pyxsd.__all__
    assert not hasattr(pyxsd, "RelaxNGSchema")

    with pytest.raises(PyXSDError):
        pyxsd.compile(io.StringIO(VALID))
    xsd_schema = pyxsd.compile(io.StringIO(XSD_SCHEMA))
    assert isinstance(xsd_schema, pyxsd.Schema)
    assert not xsd_schema.report.has_errors


def test_a_returned_schema_is_not_a_document_or_xsd_schema():
    schema = compile_schema()

    assert not isinstance(schema, pyxsd.Schema)
    assert not hasattr(schema, "classes")
    assert schema.__class__.__module__ == "pyxsd.relaxng.schema"


@pytest.mark.parametrize(
    "extra",
    [
        {"xml_policy": XmlParserPolicy.default()},
        {"xml_limits": XmlLimits(max_bytes=1)},
    ],
)
def test_both_compilers_reject_the_same_policy_parser_combinations(extra, tmp_path):
    relaxng_stream = CountingStream(VALID)
    xsd_stream = CountingStream(XSD_SCHEMA)

    with pytest.raises(ValueError) as relaxng_error:
        RelaxNGSchema.compile(relaxng_stream, resource_policy=ScopePolicy(), **extra)
    with pytest.raises(ValueError) as xsd_error:
        pyxsd.Schema.compile(xsd_stream, resource_policy=ScopePolicy(), **extra)

    assert str(relaxng_error.value) == str(xsd_error.value)
    assert "cannot be combined" in str(relaxng_error.value)
    assert relaxng_stream.reads == 0
    assert xsd_stream.reads == 0

    for compiler in (RelaxNGSchema.compile, pyxsd.Schema.compile):
        with pytest.raises(ValueError, match="cannot be combined"):
            compiler(tmp_path / "missing.xml", resource_policy=ScopePolicy(), **extra)


def test_both_compilers_share_the_require_valid_message_shape():
    invalid_relaxng = compile_schema(io.StringIO(f'<grammar xmlns="{NS}"/>'))
    invalid_xsd = pyxsd.Schema.compile(io.StringIO(BROKEN_XSD_SCHEMA))

    assert invalid_relaxng.report.has_errors
    assert invalid_xsd.report.has_errors
    for schema in (invalid_relaxng, invalid_xsd):
        with pytest.raises(ValidationError) as excinfo:
            schema.require_valid()
        error = excinfo.value
        assert error.report is schema.report
        assert str(error) == (
            f"the schema is not valid: {schema.report.error_count} error(s); "
            "inspect ValidationError.report"
        )


def test_both_compilers_enter_the_policy_scope_with_the_same_root_read(tmp_path):
    path = tmp_path / "schema.xml"
    path.write_text(VALID, encoding="utf-8")

    relaxng_path_policy = ScopePolicy(VALID)
    xsd_path_policy = ScopePolicy(XSD_SCHEMA)
    RelaxNGSchema.compile(path, resource_policy=relaxng_path_policy)
    pyxsd.Schema.compile(path, resource_policy=xsd_path_policy)
    assert relaxng_path_policy.events == xsd_path_policy.events == [("enter", True), ("exit",)]

    relaxng_stream_policy = ScopePolicy(VALID)
    xsd_stream_policy = ScopePolicy(XSD_SCHEMA)
    RelaxNGSchema.compile(io.StringIO(VALID), resource_policy=relaxng_stream_policy)
    pyxsd.Schema.compile(io.StringIO(XSD_SCHEMA), resource_policy=xsd_stream_policy)
    assert (
        relaxng_stream_policy.events
        == xsd_stream_policy.events
        == [
            ("enter", False),
            ("exit",),
        ]
    )
