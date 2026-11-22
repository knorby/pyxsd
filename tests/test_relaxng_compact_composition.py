"""Nested compact resources through composition without a mixed closure.

A compact root keeps its syntax for every include and external reference,
regardless of the dependency's suffix.  These tests pin inherited compact
namespaces, overrides, cycles, policy denial, exact spans after composition,
nested malformed findings, the shared token budget, and independent concurrent
compilations.
"""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

import pytest

from pyxsd.locations import SourceTextSpan
from pyxsd.relaxng.compact.lexer import lex_compact
from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.names import Name
from pyxsd.relaxng.patterns import Element, Empty, Pattern, Ref
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.resource_policy import ResourceData, ResourcePolicyError


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def deref(pattern: Pattern) -> Pattern:
    while isinstance(pattern, Ref):
        pattern = pattern.target
    return pattern


def error_codes(schema: RelaxNGSchema) -> list[str]:
    return [issue.code for issue in schema.report.errors]


def issue(schema: RelaxNGSchema, code: str):
    matches = [item for item in schema.report.errors if item.code == code]
    assert len(matches) == 1
    return matches[0]


def token_count(path: Path) -> int:
    tokens = lex_compact(
        path.read_bytes(),
        source_id=str(path.resolve()),
        budget=BudgetCounter("compile"),
    )
    return len(tokens) - 1


def test_a_compact_root_reads_compact_includes_and_externals(tmp_path):
    root = write(
        tmp_path / "root.rnc",
        'include "child.data"\n\nstart = external "child.rng"\n',
    )
    child_data = write(tmp_path / "child.data", "item = element child { empty }\n")
    child_rng = write(tmp_path / "child.rng", "element external { empty }\n")

    schema = RelaxNGSchema.compile(root)

    assert not schema.report.has_errors
    assert deref(schema.grammar.start) == Element(Name(None, "external"), Empty())
    dependencies = {dependency.canonical_id: dependency for dependency in schema.dependencies}
    assert set(dependencies) == {
        str(root.resolve()),
        str(child_data.resolve()),
        str(child_rng.resolve()),
    }
    for path in (root, child_data, child_rng):
        dependency = dependencies[str(path.resolve())]
        assert dependency.content_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert [
        reference.reference for reference in dependencies[str(child_data.resolve())].references
    ] == ["child.data"]
    assert [
        reference.reference for reference in dependencies[str(child_rng.resolve())].references
    ] == ["child.rng"]


def test_a_compact_include_override_replaces_the_nested_definition(tmp_path):
    write(tmp_path / "base.rnc", "item = element original { empty }\n")
    root = write(
        tmp_path / "root.rnc",
        'include "base.rnc" { item = element overridden { empty } }\nstart = item\n',
    )

    schema = RelaxNGSchema.compile(root)

    assert not schema.report.has_errors
    assert deref(schema.grammar.start) == Element(Name(None, "overridden"), Empty())


def test_compact_include_cycles_are_schema_findings(tmp_path):
    write(tmp_path / "a.rnc", 'include "b.rnc"\nstart = element a { empty }\n')
    write(tmp_path / "b.rnc", 'include "a.rnc"\nstart = element b { empty }\n')

    schema = RelaxNGSchema.compile(tmp_path / "a.rnc")

    assert "relaxng-resource-cycle" in error_codes(schema)


class DenyingPolicy:
    """A policy that admits only its named root and denies every dependency."""

    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self.payload = root.read_bytes()

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        del base, purpose
        if source == self.root:
            return ResourceData(source, self.payload)
        raise ResourcePolicyError(f"denied {source}")

    def parse(self, data: ResourceData, context: object) -> object:
        del data, context
        raise AssertionError("no resource reaches the XML parser")


def test_a_denied_compact_dependency_is_a_resource_finding(tmp_path):
    root = write(tmp_path / "root.rnc", 'start = external "denied.rnc"\n')

    schema = RelaxNGSchema.compile(root, resource_policy=DenyingPolicy(root))

    finding = issue(schema, "relaxng-resource-read")
    assert "denied" in finding.message
    assert [dependency.canonical_id for dependency in schema.dependencies] == [str(root.resolve())]


def test_repeated_compact_resources_keep_distinct_inherited_namespaces(tmp_path):
    write(tmp_path / "child.rnc", "element child { empty }\n")
    root = write(
        tmp_path / "root.rnc",
        'namespace one = "urn:one"\n'
        'namespace two = "urn:two"\n'
        'start = (external "child.rnc" inherit = one | external "child.rnc" inherit = two)\n',
    )

    schema = RelaxNGSchema.compile(root)

    assert not schema.report.has_errors
    names = {
        definition.pattern.name_class
        for definition in schema.grammar.definitions.values()
        if isinstance(definition.pattern.name_class, Name)
    }
    assert names == {Name("urn:one", "child"), Name("urn:two", "child")}
    assert len(schema.dependencies) == 2


def test_undefined_references_keep_their_exact_compact_span_after_composition(tmp_path):
    root = write(tmp_path / "schema.rnc", "start = missing")

    schema = RelaxNGSchema.compile(root)

    finding = issue(schema, "relaxng-undefined-reference")
    assert finding.location is not None
    assert finding.location.source == str(root.resolve())
    assert finding.location.span == SourceTextSpan(8, 15, 1, 9, 1, 16)


def test_a_malformed_nested_compact_resource_becomes_a_schema_finding(tmp_path):
    write(tmp_path / "base.rnc", "start = element broken {")
    root = write(tmp_path / "root.rnc", 'include "base.rnc"\nstart = element root { empty }\n')

    schema = RelaxNGSchema.compile(root)

    finding = issue(schema, "relaxng-resource-read")
    assert finding.location is not None
    assert finding.location.source == str((tmp_path / "base.rnc").resolve())
    assert finding.location.span is not None
    assert finding.message == "expected a pattern; found end of input; expected a pattern"
    assert str((tmp_path / "base.rnc").resolve()) not in finding.message
    dependencies = {dependency.canonical_id: dependency for dependency in schema.dependencies}
    assert set(dependencies) == {
        str(root.resolve()),
        str((tmp_path / "base.rnc").resolve()),
    }
    reference = dependencies[str((tmp_path / "base.rnc").resolve())].references[0]
    assert reference.containing_id == str(root.resolve())
    assert reference.reference == "base.rnc"


def test_a_nested_compact_decode_failure_uses_the_containing_reference_location(tmp_path):
    (tmp_path / "base.rnc").write_bytes(b'start = "\xff"')
    root = write(tmp_path / "root.rnc", 'include "base.rnc"\nstart = element root { empty }\n')

    schema = RelaxNGSchema.compile(root)

    finding = issue(schema, "relaxng-resource-read")
    assert finding.location is not None
    assert finding.location.source == str(root.resolve())
    assert finding.location.path


def test_the_compact_token_budget_is_shared_across_nested_resources(tmp_path):
    root = write(tmp_path / "root.rnc", 'include "child.rnc"\nstart = element root { empty }\n')
    child = write(tmp_path / "child.rnc", "item = element child { empty }\n")
    total = token_count(root) + token_count(child)

    exact = RelaxNGSchema.compile(root, limits=RelaxNGLimits(max_compact_tokens=total))
    assert not exact.report.has_errors

    with pytest.raises(RelaxNGLimitError) as failure:
        RelaxNGSchema.compile(root, limits=RelaxNGLimits(max_compact_tokens=total - 1))
    assert failure.value.stage == "compile"
    assert failure.value.budget == "max_compact_tokens"
    assert failure.value.observed > failure.value.configured


@pytest.mark.parametrize(
    "body",
    [
        'include "http://"\nstart = element root { empty }\n',
        'start = external "http://"\n',
    ],
)
def test_invalid_compact_hrefs_are_resource_findings(tmp_path, body):
    root = write(tmp_path / "root.rnc", body)

    schema = RelaxNGSchema.compile(root)

    finding = issue(schema, "relaxng-resource-read")
    assert "not a URI reference" in finding.message
    assert [dependency.canonical_id for dependency in schema.dependencies] == [str(root.resolve())]


def test_independent_compact_compilations_do_not_share_parser_state():
    barrier = threading.Barrier(2)
    results: dict[str, RelaxNGSchema] = {}
    failures: list[BaseException] = []

    def work(name: str, source: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = RelaxNGSchema.compile(io.StringIO(source), syntax="compact")
        except BaseException as exc:
            failures.append(exc)
            barrier.abort()

    one = 'namespace p = "urn:one"\nelement root { element p:child { empty } }'
    two = 'namespace p = "urn:two"\nelement root { element p:child { empty } }'
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
    for name, uri in (("one", "urn:one"), ("two", "urn:two")):
        names = {
            definition.pattern.name_class
            for definition in results[name].grammar.definitions.values()
            if isinstance(definition.pattern.name_class, Name)
            and definition.pattern.name_class.local == "child"
        }
        assert names == {Name(uri, "child")}
